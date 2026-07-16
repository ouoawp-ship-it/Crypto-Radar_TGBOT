"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { PageTitle } from "@/components/PageTitle";
import { SignalCard } from "@/components/SignalCard";
import { SignalDetailDrawer } from "@/components/SignalDetailDrawer";
import { getRadarIntelligence, getSignals, getSignalStats, invalidatePublicApiCache } from "@/lib/api";
import { compact, formatDateTime, safeText } from "@/lib/format";
import type { OpportunityBoard, RadarIntelligence, SignalItem } from "@/lib/types";

type RadarFilters = {
  symbol: string;
  module: string;
  status: string;
  q: string;
  window_sec: string;
};

const defaultFilters: RadarFilters = { symbol: "", module: "", status: "", q: "", window_sec: "604800" };

const moduleOptions = [
  { value: "", label: "全部模块" },
  { value: "launch", label: "启动雷达" },
  { value: "funding", label: "资金费率" },
  { value: "flow", label: "资金流" },
  { value: "structure", label: "结构雷达" },
  { value: "structure_review", label: "结构复盘" },
  { value: "announcement", label: "公告" }
];

const statusOptions = [
  { value: "", label: "全部状态" },
  { value: "sent", label: "已发送" },
  { value: "blocked", label: "已阻止" },
  { value: "failed", label: "失败" },
  { value: "skipped", label: "已跳过" },
  { value: "dry_run", label: "演练" }
];

const windowOptions = [
  { value: "86400", label: "24 小时" },
  { value: "604800", label: "7 天" },
  { value: "2592000", label: "30 天" }
];

function optionLabel(options: Array<{ value: string; label: string }>, value: string) {
  return options.find((item) => item.value === value)?.label || value;
}

function countValue(record: Record<string, unknown>, ...keys: string[]) {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number") return value;
    if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  }
  return undefined;
}

function SummaryItem({ label, value, hint, tone = "neutral", loading = false }: {
  label: string;
  value: unknown;
  hint: string;
  tone?: "good" | "bad" | "info" | "neutral";
  loading?: boolean;
}) {
  const toneClass = {
    good: "text-emerald-700",
    bad: "text-red-700",
    info: "text-primary-700",
    neutral: "text-text-primary"
  }[tone];

  return (
    <div className="min-w-0 bg-white px-4 py-4 sm:px-5">
      <div className="text-xs font-semibold tracking-wide text-text-muted">{label}</div>
      {loading ? (
        <div className="mt-2 h-7 w-16 animate-pulse rounded-md bg-surface-container" />
      ) : (
        <div className={`table-number mt-1.5 text-2xl font-semibold ${toneClass}`}>{compact(value)}</div>
      )}
      <div className="mt-1 text-xs text-text-muted">{hint}</div>
    </div>
  );
}

function SignalCardSkeleton() {
  return (
    <div className="panel animate-pulse overflow-hidden p-5" aria-hidden="true">
      <div className="flex items-center justify-between gap-4">
        <div className="h-6 w-20 rounded-full bg-surface-container" />
        <div className="h-6 w-16 rounded-full bg-surface-container" />
      </div>
      <div className="mt-5 h-7 w-36 rounded-md bg-surface-container" />
      <div className="mt-3 h-4 w-3/4 rounded bg-surface-container" />
      <div className="mt-5 space-y-2">
        <div className="h-4 w-full rounded bg-surface-container" />
        <div className="h-4 w-5/6 rounded bg-surface-container" />
      </div>
      <div className="mt-6 h-10 w-full rounded-lg bg-surface-container" />
    </div>
  );
}

function OpportunityBoardCard({ board, onOpen }: { board: OpportunityBoard; onOpen: (reference: number | string) => void }) {
  const tone = { launch: "bg-primary-50 text-primary-700", resonance: "bg-violet-50 text-violet-700", funding: "bg-amber-50 text-amber-700", risk: "bg-red-50 text-red-700" }[board.key || ""] || "bg-surface-container text-text-secondary";
  return (
    <article className="panel overflow-hidden">
      <div className="border-b border-border-subtle px-4 py-4">
        <div className="flex items-center justify-between gap-3"><h3 className="text-sm font-semibold text-text-primary">{board.title}</h3><span className={`rounded-full px-2 py-1 text-[11px] font-semibold ${tone}`}>{compact(board.count || 0)}</span></div>
        <p className="mt-1.5 min-h-10 text-xs leading-5 text-text-muted">{board.description}</p>
      </div>
      <div className="divide-y divide-border-subtle">
        {board.items?.length ? board.items.slice(0, 4).map((entry) => {
          const signal = entry.signal || {};
          const reference = signal.public_ref || signal.id;
          return (
            <button className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition hover:bg-surface-bright" disabled={!reference} key={String(reference || `${signal.symbol}-${signal.time}`)} onClick={() => reference && onOpen(reference)}>
              <span className="min-w-0"><span className="table-number block truncate text-sm font-semibold text-text-primary">{safeText(signal.symbol, "全局")}</span><span className="mt-0.5 block truncate text-[11px] text-text-muted">{safeText(entry.intelligence?.lifecycle?.label, signal.display?.module_label)} · {formatDateTime(signal.time)}</span></span>
              <span className="shrink-0 text-primary-700">→</span>
            </button>
          );
        }) : <div className="px-4 py-6 text-center text-xs text-text-muted">当前窗口暂无候选</div>}
      </div>
    </article>
  );
}

export default function RadarPage() {
  const [draftFilters, setDraftFilters] = useState<RadarFilters>(defaultFilters);
  const [appliedFilters, setAppliedFilters] = useState<RadarFilters>(defaultFilters);
  const [signals, setSignals] = useState<SignalItem[]>([]);
  const [signalCount, setSignalCount] = useState(0);
  const [stats, setStats] = useState<Record<string, unknown>>({});
  const [intelligence, setIntelligence] = useState<RadarIntelligence>({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [selectedSignalId, setSelectedSignalId] = useState<number | string>("");

  async function load(nextFilters: RadarFilters, refresh = false) {
    if (refresh) invalidatePublicApiCache();
    setLoading(true);
    setError("");
    setAppliedFilters(nextFilters);
    try {
      const windowSec = Number(nextFilters.window_sec || 86400);
      const list = await getSignals({ ...nextFilters, limit: 40 });
      const [statPayload, intelligencePayload] = await Promise.all([
        getSignalStats(windowSec).catch(() => ({})),
        getRadarIntelligence(windowSec, 5).catch(() => ({ data_status: "degraded", items: [], boards: [] } as RadarIntelligence))
      ]);
      const items = list.items || [];
      const intelligenceByReference = new Map<string, NonNullable<NonNullable<RadarIntelligence["items"]>[number]["intelligence"]>>();
      for (const entry of intelligencePayload.items || []) {
        const reference = entry.signal?.public_ref || entry.signal?.id;
        if (reference && entry.intelligence) intelligenceByReference.set(String(reference), entry.intelligence);
      }
      setSignals(items.map((item) => ({ ...item, intelligence: intelligenceByReference.get(String(item.public_ref || item.id || "")) })));
      setSignalCount(list.count ?? items.length);
      setStats(statPayload);
      setIntelligence(intelligencePayload);
    } catch (err) {
      setError(err instanceof Error ? err.message : "信号雷达加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const syncFromUrl = () => {
      const params = new URLSearchParams(window.location.search);
      const symbol = (params.get("symbol") || "").toUpperCase();
      const nextFilters = { ...defaultFilters, symbol };
      const signalId = (params.get("signal") || "").trim();
      setDraftFilters(nextFilters);
      setSelectedSignalId(signalId);
      void load(nextFilters);
    };
    syncFromUrl();
    window.addEventListener("popstate", syncFromUrl);
    return () => window.removeEventListener("popstate", syncFromUrl);
  }, []);

  function selectSignal(signalId: number | string) {
    const reference = String(signalId || "").trim();
    if (!reference) return;
    const url = new URL(window.location.href);
    url.searchParams.set("signal", reference);
    window.history.pushState({}, "", url);
    setSelectedSignalId(reference);
  }

  function closeSignal() {
    const url = new URL(window.location.href);
    url.searchParams.delete("signal");
    window.history.replaceState({}, "", url);
    setSelectedSignalId("");
  }

  const activeFilters = useMemo(() => {
    const items: Array<{ key: keyof RadarFilters; label: string }> = [];
    if (appliedFilters.symbol) items.push({ key: "symbol", label: `币种：${appliedFilters.symbol}` });
    if (appliedFilters.q) items.push({ key: "q", label: `关键词：${appliedFilters.q}` });
    if (appliedFilters.module) items.push({ key: "module", label: optionLabel(moduleOptions, appliedFilters.module) });
    if (appliedFilters.status) items.push({ key: "status", label: optionLabel(statusOptions, appliedFilters.status) });
    return items;
  }, [appliedFilters]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void load(draftFilters, true);
  }

  function reset() {
    setDraftFilters(defaultFilters);
    void load(defaultFilters, true);
  }

  function clearAppliedFilter(key: keyof RadarFilters) {
    const next = { ...appliedFilters, [key]: "" };
    setDraftFilters(next);
    void load(next, true);
  }

  const total = intelligence.summary?.signals ?? countValue(stats, "total", "count", "signals_count");
  const sent = countValue(stats, "sent", "sent_count");
  const blocked = countValue(stats, "blocked", "blocked_count") || 0;
  const failed = countValue(stats, "failed", "failed_count") || 0;
  const skipped = countValue(stats, "skipped", "skipped_count") || 0;
  const initialLoading = loading && !signals.length && !error;

  return (
    <div className="space-y-5">
      <PageTitle
        title="信号雷达"
        subtitle="从全局机会榜进入具体信号，用排名、共振、生命周期与市场证据判断优先级。"
        tags={["机会优先", "可解释排名", "跨模块共振"]}
      />

      <form className="panel overflow-hidden" onSubmit={submit}>
        <div className="flex flex-col gap-2 border-b border-border-subtle px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-5">
          <div>
            <h2 className="section-title">筛选条件</h2>
            <p className="mt-1 text-sm text-text-muted">组合条件缩小信号范围，按回车即可应用筛选。</p>
          </div>
          <div className="flex items-center gap-2 text-xs text-text-muted">
            <span className={`h-2 w-2 rounded-full ${loading ? "animate-pulse bg-warn" : "bg-good"}`} />
            {loading ? "正在同步公开数据" : `数据已更新 · ${optionLabel(windowOptions, appliedFilters.window_sec)}`}
          </div>
        </div>

        <div className="grid gap-4 px-4 py-5 sm:grid-cols-2 sm:px-5 xl:grid-cols-12">
          <label className="block sm:col-span-1 xl:col-span-2">
            <span className="mb-2 block text-xs font-semibold text-text-secondary">币种</span>
            <input
              className="input w-full"
              placeholder="BTC 或 BTCUSDT"
              value={draftFilters.symbol}
              onChange={(event) => setDraftFilters({ ...draftFilters, symbol: event.target.value.toUpperCase() })}
            />
          </label>

          <label className="block sm:col-span-1 xl:col-span-3">
            <span className="mb-2 block text-xs font-semibold text-text-secondary">关键词</span>
            <input
              className="input w-full"
              placeholder="搜索信号标题或摘要"
              value={draftFilters.q}
              onChange={(event) => setDraftFilters({ ...draftFilters, q: event.target.value })}
            />
          </label>

          <label className="block xl:col-span-2">
            <span className="mb-2 block text-xs font-semibold text-text-secondary">信号模块</span>
            <select className="input w-full" value={draftFilters.module} onChange={(event) => setDraftFilters({ ...draftFilters, module: event.target.value })}>
              {moduleOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select>
          </label>

          <label className="block xl:col-span-2">
            <span className="mb-2 block text-xs font-semibold text-text-secondary">发送状态</span>
            <select className="input w-full" value={draftFilters.status} onChange={(event) => setDraftFilters({ ...draftFilters, status: event.target.value })}>
              {statusOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select>
          </label>

          <fieldset className="sm:col-span-2 xl:col-span-3">
            <legend className="mb-2 text-xs font-semibold text-text-secondary">时间窗口</legend>
            <div className="grid grid-cols-3 gap-1 rounded-lg bg-surface-container p-1">
              {windowOptions.map((item) => {
                const selected = draftFilters.window_sec === item.value;
                return (
                  <button
                    key={item.value}
                    type="button"
                    aria-pressed={selected}
                    className={`h-10 rounded-md px-2 text-xs font-semibold transition sm:h-8 ${selected ? "bg-white text-primary-700 shadow-soft" : "text-text-secondary hover:text-text-primary"}`}
                    onClick={() => setDraftFilters({ ...draftFilters, window_sec: item.value })}
                  >
                    {item.label}
                  </button>
                );
              })}
            </div>
          </fieldset>
        </div>

        <div className="flex flex-col gap-3 border-t border-border-subtle bg-surface-bright px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-5">
          <div className="flex min-h-8 flex-wrap items-center gap-2">
            {activeFilters.length ? activeFilters.map((item) => (
              <button
                className="chip gap-1.5 transition hover:border-primary-100 hover:text-primary-700"
                key={item.key}
                type="button"
                onClick={() => clearAppliedFilter(item.key)}
                aria-label={`移除筛选：${item.label}`}
              >
                {item.label}<span aria-hidden="true">×</span>
              </button>
            )) : <span className="text-xs text-text-muted">当前显示全部公开信号</span>}
          </div>
          <div className="grid grid-cols-[1fr_auto] gap-2 sm:flex">
            <button className="btn min-w-28" type="submit" disabled={loading}>
              {loading ? "筛选中..." : "应用筛选"}
            </button>
            <button className="btn-secondary" type="button" onClick={reset} disabled={loading}>
              重置
            </button>
          </div>
        </div>
      </form>

      <section className="panel grid grid-cols-2 gap-px overflow-hidden bg-border-subtle md:grid-cols-4">
        <SummaryItem label="有效信号" value={total} hint={optionLabel(windowOptions, appliedFilters.window_sec)} tone="info" loading={initialLoading} />
        <SummaryItem label="活跃币种" value={intelligence.summary?.symbols} hint="每币保留最新状态" tone="good" loading={initialLoading} />
        <SummaryItem label="共振币种" value={intelligence.summary?.resonance_symbols} hint="至少两个雷达模块" tone="info" loading={initialLoading} />
        <SummaryItem label="正在增强" value={intelligence.summary?.enhancing_symbols} hint="规则分数较上次提高" tone="good" loading={initialLoading} />
      </section>

      {(blocked + failed > 0 || skipped > 0 || Number(sent || 0) === 0) && !initialLoading ? (
        <p className="px-1 text-xs text-text-muted">投递状态：已发送 {compact(sent)} · 阻止/失败 {compact(blocked + failed)} · 跳过 {compact(skipped)}。发送状态仅用于运维复核，不作为市场强弱依据。</p>
      ) : null}

      {error ? <ErrorState message={error} onRetry={() => load(appliedFilters, true)} /> : null}

      {!error ? (
        <section>
          <div className="mb-4"><h2 className="text-lg font-semibold text-text-primary">机会看板</h2><p className="mt-1 text-sm text-text-muted">四个收敛入口覆盖启动、跨模块共振、极端费率和结构风险；空白表示当前窗口没有足够证据。</p></div>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            {(intelligence.boards || []).map((board) => <OpportunityBoardCard board={board} key={board.key} onOpen={selectSignal} />)}
          </div>
          {!loading && intelligence.data_status === "empty" ? <div className="panel mt-4 border-dashed p-5 text-sm text-text-muted">当前窗口还没有已发送信号。系统会继续扫描；无需用演练或失败记录填充机会榜。</div> : null}
        </section>
      ) : null}

      <section>
        <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-semibold text-text-primary">最新信号</h2>
              {!initialLoading && !error ? <span className="chip">{compact(signalCount)} 条</span> : null}
            </div>
            <p className="mt-1 text-sm text-text-muted">按信号时间倒序排列，快速查看模块、状态与摘要。</p>
          </div>
          <span className="text-xs font-semibold text-text-muted">最新优先 · 最多展示 40 条</span>
        </div>

        {initialLoading ? (
          <div className="grid gap-4 xl:grid-cols-2">
            {Array.from({ length: 4 }).map((_, index) => <SignalCardSkeleton key={index} />)}
          </div>
        ) : (
          <div className="grid gap-4 xl:grid-cols-2">
            {signals.map((item) => (
              <SignalCard key={item.public_ref || item.id || `${item.symbol}-${item.time}`} item={item} context="radar" onOpen={(selected) => {
                const reference = selected.public_ref || selected.id;
                if (reference) selectSignal(reference);
              }} />
            ))}
          </div>
        )}

        {!loading && !error && !signals.length ? (
          <EmptyState title="没有匹配的公开信号" text="尝试减少筛选条件、扩大时间窗口，或清除币种与关键词后重新搜索。" />
        ) : null}
      </section>

      {selectedSignalId ? (
        <SignalDetailDrawer signalId={selectedSignalId} onClose={closeSignal} onSelectSignal={selectSignal} />
      ) : null}
    </div>
  );
}
