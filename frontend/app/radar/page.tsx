"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { ErrorState } from "@/components/ErrorState";
import { LegacySignalRadar } from "@/components/LegacySignalRadar";
import { SignalDetailDrawer } from "@/components/SignalDetailDrawer";
import {
  getMarketOverview,
  getRadarBoards,
  getRadarIntelligence,
  getSignals,
  getSignalStats
} from "@/lib/api";
import { compact, formatDateTime, safeText } from "@/lib/format";
import { cockpitV2Enabled } from "@/lib/features";
import type {
  CockpitBoard,
  CockpitBoardItem,
  MarketOverview,
  OpportunityBoard,
  RadarBoards,
  RadarIntelligence,
  SignalIntelligence,
  SignalItem
} from "@/lib/types";

type RadarFilters = {
  symbol: string;
  module: string;
  status: string;
  q: string;
  window_sec: string;
};

type RadarSnapshot = {
  signals: SignalItem[];
  signalCount: number;
  stats: Record<string, unknown>;
  intelligence: RadarIntelligence;
  overview: MarketOverview;
  boards: RadarBoards;
  loadedAt: Date;
};

const defaultFilters: RadarFilters = { symbol: "", module: "", status: "sent", q: "", window_sec: "3600" };

function emptyRadarSnapshot(): RadarSnapshot {
  return { signals: [], signalCount: 0, stats: {}, intelligence: {}, overview: {}, boards: {}, loadedAt: new Date(0) };
}

const moduleOptions = [
  { value: "", label: "全部事件" },
  { value: "launch", label: "启动" },
  { value: "funding", label: "资金费率" },
  { value: "flow", label: "资金流" },
  { value: "announcement", label: "公告" }
];

const statusOptions = [
  { value: "sent", label: "已发送" },
  { value: "", label: "全部状态" },
  { value: "blocked", label: "已阻止" },
  { value: "failed", label: "失败" },
  { value: "skipped", label: "已跳过" },
  { value: "dry_run", label: "演练" }
];

const marketWindows = [
  { value: "900", label: "15m" },
  { value: "1800", label: "30m" },
  { value: "3600", label: "1h" },
  { value: "14400", label: "4h" },
  { value: "86400", label: "1d" }
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

function signedPercent(value: unknown, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${number > 0 ? "+" : ""}${number.toFixed(digits)}%`;
}

function money(value: unknown, signed = true) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const sign = signed ? (number > 0 ? "+" : number < 0 ? "−" : "") : "";
  const absolute = Math.abs(number);
  if (absolute >= 1_000_000_000) return `${sign}$${(absolute / 1_000_000_000).toFixed(2)}B`;
  if (absolute >= 1_000_000) return `${sign}$${(absolute / 1_000_000).toFixed(1)}M`;
  if (absolute >= 1_000) return `${sign}$${(absolute / 1_000).toFixed(1)}K`;
  return `${sign}$${absolute.toFixed(0)}`;
}

function boardValue(item: CockpitBoardItem) {
  if (item.unit === "usd") return money(item.value);
  if (item.unit === "percent_per_cycle") return signedPercent(item.value, 3);
  if (item.unit === "score") {
    const value = Number(item.value);
    if (!Number.isFinite(value)) return "—";
    return `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(1)}`;
  }
  return signedPercent(item.value);
}

function valueTone(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "text-text-primary";
  return number > 0 ? "text-emerald-700" : "text-red-700";
}

function dataStatusMeta(status?: string) {
  switch (status) {
    case "ready": return { label: "LIVE", detail: "数据就绪", className: "bg-emerald-50 text-emerald-700" };
    case "warming_up": return { label: "WARMING", detail: "历史数据预热中", className: "bg-primary-50 text-primary-700" };
    case "partial":
    case "degraded": return { label: "PARTIAL", detail: "部分指标可用", className: "bg-amber-50 text-amber-700" };
    case "stale": return { label: "STALE", detail: "数据已过期", className: "bg-red-50 text-red-700" };
    default: return { label: "WAITING", detail: "等待首批数据", className: "bg-surface-container text-text-muted" };
  }
}

function DataStatusBadge({ status }: { status?: string }) {
  const meta = dataStatusMeta(status);
  return <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${meta.className}`} title={meta.detail}>{meta.label}</span>;
}

function durationText(value?: number) {
  const seconds = Math.max(0, Number(value || 0));
  if (!seconds) return "0 分钟";
  const days = Math.floor(seconds / 86400);
  if (days) return `${days} 天`;
  const hours = Math.floor(seconds / 3600);
  if (hours) return `${hours} 小时`;
  return `${Math.max(1, Math.floor(seconds / 60))} 分钟`;
}

function RankBadge({ rank, label }: { rank?: SignalIntelligence["self_rank"]; label: string }) {
  if (!rank?.available) return <span className="rounded border border-border-subtle px-1.5 py-0.5 text-[10px] text-text-muted" title={rank?.reason}>{label} —</span>;
  return (
    <span className="rounded border border-border-subtle bg-surface-bright px-1.5 py-0.5 text-[10px] font-medium text-text-secondary" title={rank.method}>
      {label} P{Math.round(Number(rank.percentile || 0))} · #{rank.rank}/{rank.sample_size}
    </span>
  );
}

function EventRow({ item, onOpen }: { item: SignalItem; onOpen: (reference: number | string) => void }) {
  const reference = item.public_ref || item.id;
  const intelligence = item.intelligence;
  return (
    <button
      aria-label={`${safeText(item.symbol, "全局信号")} 查看证据与上下文`}
      className="group w-full border-b border-border-subtle px-3 py-3 text-left transition last:border-b-0 hover:bg-surface-bright"
      disabled={!reference}
      onClick={() => reference && onOpen(reference)}
      type="button"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="table-number text-sm font-semibold text-text-primary">{safeText(item.symbol, "GLOBAL")}</span>
            <span className="truncate rounded bg-primary-50 px-1.5 py-0.5 text-[10px] font-semibold text-primary-700">{safeText(item.display?.module_label, item.module)}</span>
          </div>
          <div className="mt-1 truncate text-[11px] text-text-muted">{formatDateTime(item.time)} · {safeText(intelligence?.lifecycle?.label, item.display?.status_label)}</div>
        </div>
        <span className="table-number shrink-0 text-xs font-semibold text-text-secondary">{item.score !== null && item.score !== undefined ? `${item.score}分` : "—"}</span>
      </div>
      <p className="mt-2 line-clamp-2 text-xs leading-5 text-text-secondary">{safeText(item.display?.summary || item.excerpt, "等待公开摘要")}</p>
      <div className="mt-2 flex flex-wrap gap-1">
        <RankBadge label="自身" rank={intelligence?.self_rank} />
        <RankBadge label="全场" rank={intelligence?.market_strength_rank} />
      </div>
      <div className="mt-2 flex items-center justify-between text-[10px] text-text-muted">
        <span>{intelligence?.resonance?.active_count ? `${intelligence.resonance.active_count} 个周期共振` : "暂无跨周期共振"}</span>
        <span className="font-semibold text-primary-700 opacity-0 transition group-hover:opacity-100">查看 →</span>
      </div>
    </button>
  );
}

function BoardSide({ side, positive, onSymbol }: {
  side?: CockpitBoard["positive"];
  positive: boolean;
  onSymbol: (symbol: string) => void;
}) {
  const items = side?.items || [];
  return (
    <div className="min-w-0">
      <div className={`border-b border-border-subtle px-2.5 py-2 text-[11px] font-semibold ${positive ? "text-emerald-700" : "text-red-700"}`}>{side?.title || (positive ? "正向" : "负向")}</div>
      <div className="divide-y divide-border-subtle">
        {items.length ? items.slice(0, 6).map((item, index) => (
          <button
            className="grid w-full grid-cols-[18px_minmax(0,1fr)_auto] items-center gap-1.5 px-2.5 py-2 text-left transition hover:bg-surface-bright"
            key={`${item.symbol}-${index}`}
            onClick={() => item.symbol && onSymbol(item.symbol)}
            type="button"
          >
            <span className="table-number text-[10px] text-text-muted">{index + 1}</span>
            <span className="min-w-0">
              <span className="table-number block truncate text-xs font-semibold text-text-primary">{safeText(item.coin || item.symbol)}</span>
              <span className="table-number mt-0.5 block truncate text-[9px] text-text-muted">强度 P{Math.round(Number(item.strength_percentile || 0))}</span>
            </span>
            <span className={`table-number text-[11px] font-semibold ${valueTone(item.value)}`}>{boardValue(item)}</span>
          </button>
        )) : <div className="px-2 py-6 text-center text-[10px] leading-4 text-text-muted">等待有效样本</div>}
      </div>
    </div>
  );
}

function MarketBoardCard({ board, onSymbol }: { board: CockpitBoard; onSymbol: (symbol: string) => void }) {
  return (
    <article className="cockpit-panel min-w-0">
      <div className="cockpit-panel-header">
        <div>
          <h3 className="text-xs font-semibold text-text-primary">{safeText(board.title, "市场榜单")}</h3>
          <div className="mt-0.5 text-[10px] text-text-muted">覆盖 {compact(board.coverage || 0)} 个资产</div>
        </div>
        <span className={`h-1.5 w-1.5 rounded-full ${board.available ? "bg-good" : "bg-warn"}`} title={board.available ? "数据可用" : board.reason} />
      </div>
      <div className="grid grid-cols-2 divide-x divide-border-subtle">
        <BoardSide onSymbol={onSymbol} positive side={board.positive} />
        <BoardSide onSymbol={onSymbol} positive={false} side={board.negative} />
      </div>
      {!board.available && board.reason ? <div className="border-t border-border-subtle bg-amber-50/40 px-2.5 py-2 text-[10px] text-amber-700">{board.reason}</div> : null}
    </article>
  );
}

function LoadingBoard() {
  return (
    <div className="cockpit-panel animate-pulse p-3" aria-hidden="true">
      <div className="h-4 w-28 rounded bg-surface-container" />
      <div className="mt-4 grid grid-cols-2 gap-3">
        <div className="h-44 rounded bg-surface-container-low" />
        <div className="h-44 rounded bg-surface-container-low" />
      </div>
    </div>
  );
}

function MetricLine({ label, value, tone = "neutral" }: { label: string; value: string; tone?: "good" | "bad" | "neutral" }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-border-subtle px-3 py-2.5 last:border-b-0">
      <span className="text-[11px] text-text-muted">{label}</span>
      <span className={`table-number text-xs font-semibold ${tone === "good" ? "text-emerald-700" : tone === "bad" ? "text-red-700" : "text-text-primary"}`}>{value}</span>
    </div>
  );
}

function MarketStatePanel({ overview }: { overview: MarketOverview }) {
  const state = overview.overview || {};
  const bias = {
    inflow: ["资金偏流入", "text-emerald-700", "bg-emerald-500"],
    outflow: ["资金偏流出", "text-red-700", "bg-red-500"],
    broad_up: ["市场广度偏强", "text-emerald-700", "bg-emerald-500"],
    broad_down: ["市场广度偏弱", "text-red-700", "bg-red-500"],
    mixed: ["市场方向分歧", "text-amber-700", "bg-amber-500"]
  }[state.bias || "mixed"] || ["市场方向分歧", "text-amber-700", "bg-amber-500"];
  return (
    <section className="cockpit-panel">
      <div className="cockpit-panel-header">
        <div>
          <h2 className="text-xs font-semibold text-text-primary">全场态势</h2>
          <p className="mt-0.5 text-[10px] text-text-muted">结构化市场快照</p>
        </div>
        <span className={`inline-flex items-center gap-1.5 text-[10px] font-semibold ${bias[1]}`}><span className={`h-1.5 w-1.5 rounded-full ${bias[2]}`} />{bias[0]}</span>
      </div>
      <MetricLine label="上涨 / 下跌" value={`${compact(state.advancing || 0)} / ${compact(state.declining || 0)}`} />
      <MetricLine label="市场广度" value={signedPercent(state.breadth_pct)} tone={Number(state.breadth_pct) > 0 ? "good" : Number(state.breadth_pct) < 0 ? "bad" : "neutral"} />
      <MetricLine label="现货主动资金" value={money(state.spot_net_flow_usd)} tone={Number(state.spot_net_flow_usd) > 0 ? "good" : Number(state.spot_net_flow_usd) < 0 ? "bad" : "neutral"} />
      <MetricLine label="合约主动资金" value={money(state.futures_net_flow_usd)} tone={Number(state.futures_net_flow_usd) > 0 ? "good" : Number(state.futures_net_flow_usd) < 0 ? "bad" : "neutral"} />
      <MetricLine label="24h 成交额覆盖" value={money(state.total_quote_volume, false)} />
    </section>
  );
}

function buildTendencies(boards: CockpitBoard[]) {
  const values = new Map<string, { symbol: string; hits: number; score: number }>();
  for (const board of boards.filter((item) => ["price", "oi", "futures_flow", "spot_flow"].includes(item.key || ""))) {
    for (const item of board.positive?.items || []) {
      if (!item.symbol) continue;
      const current = values.get(item.symbol) || { symbol: item.symbol, hits: 0, score: 0 };
      current.hits += 1;
      current.score += 1;
      values.set(item.symbol, current);
    }
    for (const item of board.negative?.items || []) {
      if (!item.symbol) continue;
      const current = values.get(item.symbol) || { symbol: item.symbol, hits: 0, score: 0 };
      current.hits += 1;
      current.score -= 1;
      values.set(item.symbol, current);
    }
  }
  return [...values.values()].filter((item) => item.hits >= 2).sort((a, b) => b.hits - a.hits || Math.abs(b.score) - Math.abs(a.score)).slice(0, 8);
}

function TendencyPanel({ boards, onSymbol }: { boards: CockpitBoard[]; onSymbol: (symbol: string) => void }) {
  const tendencies = useMemo(() => buildTendencies(boards), [boards]);
  return (
    <section className="cockpit-panel">
      <div className="cockpit-panel-header">
        <div><h2 className="text-xs font-semibold text-text-primary">资金倾向性</h2><p className="mt-0.5 text-[10px] text-text-muted">多榜合流，不等于交易方向</p></div>
        <span className="text-[10px] text-text-muted">TOP {tendencies.length}</span>
      </div>
      <div className="divide-y divide-border-subtle">
        {tendencies.length ? tendencies.map((item, index) => {
          const label = item.score >= 2 ? "偏强合流" : item.score <= -2 ? "偏弱合流" : "方向分歧";
          const tone = item.score >= 2 ? "text-emerald-700" : item.score <= -2 ? "text-red-700" : "text-amber-700";
          return (
            <button className="grid w-full grid-cols-[20px_1fr_auto] items-center gap-2 px-3 py-2.5 text-left transition hover:bg-surface-bright" key={item.symbol} onClick={() => onSymbol(item.symbol)} type="button">
              <span className="table-number text-[10px] text-text-muted">{index + 1}</span>
              <span className="table-number text-xs font-semibold text-text-primary">{item.symbol.replace("USDT", "")}</span>
              <span className={`text-[10px] font-semibold ${tone}`}>{item.hits}榜 · {label}</span>
            </button>
          );
        }) : <div className="px-3 py-8 text-center text-xs text-text-muted">等待至少两个榜单出现同币种</div>}
      </div>
    </section>
  );
}

function OpportunityList({ boards, onOpen }: { boards: OpportunityBoard[]; onOpen: (reference: number | string) => void }) {
  return (
    <section className="cockpit-panel">
      <div className="cockpit-panel-header"><div><h2 className="text-xs font-semibold text-text-primary">机会看板</h2><p className="mt-0.5 text-[10px] text-text-muted">规则事件的收敛入口</p></div></div>
      <div className="grid divide-y divide-border-subtle md:grid-cols-2 md:divide-x md:divide-y-0 2xl:grid-cols-4">
        {boards.map((board) => (
          <div className="min-w-0 p-3" key={board.key}>
            <div className="flex items-center justify-between gap-2"><h3 className="truncate text-xs font-semibold text-text-primary">{board.title}</h3><span className="table-number rounded bg-surface-container px-1.5 py-0.5 text-[10px] text-text-muted">{compact(board.count || 0)}</span></div>
            <div className="mt-2 space-y-1">
              {(board.items || []).slice(0, 3).map((entry) => {
                const signal = entry.signal || {};
                const reference = signal.public_ref || signal.id;
                return (
                  <button className="flex w-full items-center justify-between gap-2 rounded px-1.5 py-1.5 text-left hover:bg-surface-bright" disabled={!reference} key={String(reference || signal.symbol)} onClick={() => reference && onOpen(reference)} type="button">
                    <span className="table-number truncate text-[11px] font-semibold text-text-secondary">{safeText(signal.symbol, "全局")}</span>
                    <span className="truncate text-[9px] text-text-muted">{safeText(entry.intelligence?.lifecycle?.label)}</span>
                  </button>
                );
              })}
              {!board.items?.length ? <div className="py-3 text-center text-[10px] text-text-muted">暂无候选</div> : null}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function CockpitRadarPage() {
  const [draftFilters, setDraftFilters] = useState<RadarFilters>(defaultFilters);
  const [appliedFilters, setAppliedFilters] = useState<RadarFilters>(defaultFilters);
  const [snapshot, setSnapshot] = useState<RadarSnapshot>(emptyRadarSnapshot);
  const [pendingSnapshot, setPendingSnapshot] = useState<RadarSnapshot | null>(null);
  const [incomingCount, setIncomingCount] = useState(0);
  const [error, setError] = useState("");
  const [marketError, setMarketError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [paused, setPaused] = useState(false);
  const [streamState, setStreamState] = useState<"connecting" | "live" | "reconnecting" | "paused">("connecting");
  const [selectedSignalId, setSelectedSignalId] = useState<number | string>("");
  const signalRefs = useRef<Set<string>>(new Set());
  const requestRef = useRef(0);
  const streamRefreshTimerRef = useRef<number | null>(null);

  async function fetchSnapshot(nextFilters: RadarFilters, refresh = false): Promise<RadarSnapshot> {
    const windowSec = Number(nextFilters.window_sec || 3600);
    const fetchOptions = refresh ? { bypassCache: true } : undefined;
    const list = await getSignals({ ...nextFilters, limit: 60 }, fetchOptions);
    const items = list.items || [];
    const refs = items.map((item) => item.public_ref || item.id || "").filter(Boolean);
    const [statPayload, intelligencePayload, overviewPayload, boardPayload] = await Promise.all([
      getSignalStats(Math.max(windowSec, 3600), fetchOptions).catch(() => ({})),
      getRadarIntelligence(Math.max(windowSec, 3600), 5, refs, fetchOptions).catch(() => ({ data_status: "degraded", items: [], boards: [] } as RadarIntelligence)),
      getMarketOverview(windowSec, fetchOptions).catch(() => ({ data_status: "empty", warnings: ["市场总览暂时不可用"] } as MarketOverview)),
      getRadarBoards(windowSec, 8, fetchOptions).catch(() => ({ data_status: "empty", warnings: ["雷达榜单暂时不可用"], boards: [] } as RadarBoards))
    ]);
    const intelligenceByReference = new Map<string, SignalIntelligence>();
    for (const entry of intelligencePayload.items || []) {
      const reference = entry.signal?.public_ref || entry.signal?.id;
      if (reference && entry.intelligence) intelligenceByReference.set(String(reference), entry.intelligence);
    }
    return {
      signals: items.map((item) => ({ ...item, intelligence: intelligenceByReference.get(String(item.public_ref || item.id || "")) })),
      signalCount: list.count ?? items.length,
      stats: statPayload,
      intelligence: intelligencePayload,
      overview: overviewPayload,
      boards: boardPayload,
      loadedAt: new Date()
    };
  }

  function commit(next: RadarSnapshot) {
    signalRefs.current = new Set(next.signals.map((item) => String(item.public_ref || item.id || "")).filter(Boolean));
    setSnapshot(next);
    setPendingSnapshot(null);
    setIncomingCount(0);
  }

  async function load(nextFilters: RadarFilters, options: { refresh?: boolean; background?: boolean } = {}) {
    const request = ++requestRef.current;
    const filtersChanged = Object.keys(defaultFilters).some((key) => (
      nextFilters[key as keyof RadarFilters] !== appliedFilters[key as keyof RadarFilters]
    ));
    if (options.background) setRefreshing(true); else setLoading(true);
    setError("");
    setMarketError("");
    if (!options.background && filtersChanged) {
      setSnapshot(emptyRadarSnapshot());
      setPendingSnapshot(null);
      setIncomingCount(0);
      signalRefs.current.clear();
    }
    setAppliedFilters(nextFilters);
    try {
      const next = await fetchSnapshot(nextFilters, Boolean(options.refresh));
      if (request !== requestRef.current) return;
      if (options.background && signalRefs.current.size) {
        const added = next.signals.filter((item) => !signalRefs.current.has(String(item.public_ref || item.id || ""))).length;
        if (added > 0) {
          setPendingSnapshot(next);
          setIncomingCount(added);
        } else {
          commit(next);
        }
      } else {
        commit(next);
      }
      if (next.overview.data_status === "empty" || next.boards.data_status === "empty") setMarketError("市场聚合数据正在积累，信号事件仍可正常使用。");
    } catch (loadError) {
      if (request === requestRef.current) setError(loadError instanceof Error ? loadError.message : "信号雷达加载失败");
    } finally {
      if (request === requestRef.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }

  useEffect(() => {
    const syncFromUrl = () => {
      const params = new URLSearchParams(window.location.search);
      const requestedWindow = params.get("window") || defaultFilters.window_sec;
      const nextFilters: RadarFilters = {
        symbol: (params.get("symbol") || "").toUpperCase(),
        module: params.get("module") || "",
        status: params.get("status") ?? defaultFilters.status,
        q: params.get("q") || "",
        window_sec: marketWindows.some((item) => item.value === requestedWindow) ? requestedWindow : defaultFilters.window_sec
      };
      setDraftFilters(nextFilters);
      setSelectedSignalId((params.get("signal") || "").trim());
      void load(nextFilters);
    };
    syncFromUrl();
    window.addEventListener("popstate", syncFromUrl);
    return () => window.removeEventListener("popstate", syncFromUrl);
  }, []);

  useEffect(() => {
    if (paused) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load(appliedFilters, { background: true });
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [paused, appliedFilters]);

  useEffect(() => {
    if (paused) {
      setStreamState("paused");
      return;
    }
    if (typeof EventSource === "undefined") {
      setStreamState("reconnecting");
      return;
    }
    const source = new EventSource("/public-api/stream?stream_sec=55");
    setStreamState("connecting");
    source.onopen = () => setStreamState("live");
    source.addEventListener("signal", () => {
      setStreamState("live");
      if (document.visibilityState !== "visible") return;
      if (streamRefreshTimerRef.current !== null) window.clearTimeout(streamRefreshTimerRef.current);
      streamRefreshTimerRef.current = window.setTimeout(() => {
        streamRefreshTimerRef.current = null;
        void load(appliedFilters, { refresh: true, background: true });
      }, 750);
    });
    source.addEventListener("status", () => setStreamState("live"));
    source.onerror = () => setStreamState("reconnecting");
    return () => {
      source.close();
      if (streamRefreshTimerRef.current !== null) {
        window.clearTimeout(streamRefreshTimerRef.current);
        streamRefreshTimerRef.current = null;
      }
    };
  }, [paused, appliedFilters]);

  function syncFilterUrl(filters: RadarFilters) {
    const url = new URL(window.location.href);
    for (const [key, value] of Object.entries(filters)) {
      const queryKey = key === "window_sec" ? "window" : key;
      if (value && !(key === "status" && value === defaultFilters.status)) url.searchParams.set(queryKey, value);
      else url.searchParams.delete(queryKey);
    }
    window.history.replaceState({}, "", url);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    syncFilterUrl(draftFilters);
    void load(draftFilters, { refresh: true });
  }

  function reset() {
    setDraftFilters(defaultFilters);
    syncFilterUrl(defaultFilters);
    void load(defaultFilters, { refresh: true });
  }

  function changeWindow(value: string) {
    const next = { ...draftFilters, window_sec: value };
    setDraftFilters(next);
    syncFilterUrl(next);
    void load(next, { refresh: true });
  }

  function selectSignal(reference: number | string) {
    const value = String(reference || "").trim();
    if (!value) return;
    const url = new URL(window.location.href);
    url.searchParams.set("signal", value);
    window.history.pushState({}, "", url);
    setSelectedSignalId(value);
  }

  function closeSignal() {
    const url = new URL(window.location.href);
    url.searchParams.delete("signal");
    window.history.replaceState({}, "", url);
    setSelectedSignalId("");
  }

  function filterSymbol(symbol: string) {
    const next = { ...draftFilters, symbol };
    setDraftFilters(next);
    syncFilterUrl(next);
    void load(next, { refresh: true });
  }

  const activeFilterCount = [appliedFilters.symbol, appliedFilters.module, appliedFilters.q, appliedFilters.status !== defaultFilters.status ? appliedFilters.status : ""].filter(Boolean).length;
  const initialLoading = loading && !snapshot.signals.length;
  const marketBoards = (snapshot.boards.boards || []).filter((board) => ["price", "oi", "futures_flow", "spot_flow", "realtime_futures_flow", "realtime_liquidations", "realtime_surge", "realtime_ambush"].includes(board.key || ""));
  const total = snapshot.intelligence.summary?.signals ?? countValue(snapshot.stats, "total", "count", "signals_count");
  const readiness = snapshot.overview.readiness || snapshot.boards.readiness;
  const warmupProgress = Math.max(0, Math.min(100, Number(readiness?.warmup_progress_pct || 0)));

  return (
    <div aria-busy={loading || refreshing} className="space-y-3">
      <header className="flex flex-col gap-3 px-0.5 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-xl font-semibold tracking-tight text-text-primary">信号雷达</h1>
            <DataStatusBadge status={snapshot.boards.data_status} />
          </div>
          <p className="mt-1 text-xs text-text-muted">全市场异动、相对排名、资金合流与生命周期证据</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-text-muted">
          <span>{snapshot.loadedAt.getTime() ? `更新 ${snapshot.loadedAt.toLocaleTimeString("zh-CN", { hour12: false })}` : "等待首次数据"}</span>
          <span>·</span><span>{compact(total || 0)} 条信号</span>
          <span>·</span><span>{compact(snapshot.overview.coverage?.assets || 0)} 个市场资产</span>
        </div>
      </header>

      <form className="cockpit-panel" onSubmit={submit}>
        <div className="flex flex-col gap-3 p-2.5 xl:flex-row xl:items-center xl:justify-between">
          <div className="flex min-w-0 flex-1 flex-col gap-2 sm:flex-row sm:items-end">
            <label className="min-w-0 flex-1 sm:max-w-52"><span className="mb-1 block text-xs font-medium text-text-secondary">币种</span><input className="input h-11 w-full min-w-0" placeholder="BTC 或 BTCUSDT" value={draftFilters.symbol} onChange={(event) => setDraftFilters({ ...draftFilters, symbol: event.target.value.toUpperCase() })} /></label>
            <label className="min-w-0 flex-1 sm:max-w-64"><span className="mb-1 block text-xs font-medium text-text-secondary">关键词</span><input className="input h-11 w-full min-w-0" placeholder="搜索标题、摘要或关键词" value={draftFilters.q} onChange={(event) => setDraftFilters({ ...draftFilters, q: event.target.value })} /></label>
            <label className="w-full sm:w-32"><span className="mb-1 block text-xs font-medium text-text-secondary">事件类型</span><select className="input h-11 w-full" value={draftFilters.module} onChange={(event) => setDraftFilters({ ...draftFilters, module: event.target.value })}>
              {moduleOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select></label>
            <label className="w-full sm:w-28"><span className="mb-1 block text-xs font-medium text-text-secondary">发送状态</span><select className="input h-11 w-full" value={draftFilters.status} onChange={(event) => setDraftFilters({ ...draftFilters, status: event.target.value })}>
              {statusOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select></label>
            <button className="btn h-11 px-3 text-xs" disabled={loading} type="submit">应用</button>
            {activeFilterCount ? <button className="btn-secondary h-11 px-3 text-xs" onClick={reset} type="button">清除 {activeFilterCount}</button> : null}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="grid w-full grid-cols-5 rounded-md bg-surface-container p-0.5 sm:flex sm:w-auto">
              {marketWindows.map((item) => {
                const selected = draftFilters.window_sec === item.value;
                return <button aria-pressed={selected} className={`h-11 min-w-0 rounded px-2 text-xs font-semibold transition sm:min-w-11 ${selected ? "bg-surface-panel text-primary-700 shadow-soft" : "text-text-muted hover:text-text-primary"}`} key={item.value} onClick={() => changeWindow(item.value)} type="button">{item.label}</button>;
              })}
            </div>
            <button className="btn-secondary h-11 flex-1 whitespace-nowrap px-3 text-xs sm:flex-none" onClick={() => setPaused((value) => !value)} type="button">{paused ? "继续更新" : "暂停"}</button>
            <button aria-label="刷新雷达" className="btn-secondary h-11 w-11 px-0" disabled={refreshing || loading} onClick={() => void load(appliedFilters, { refresh: true, background: true })} type="button">{refreshing ? "…" : "↻"}</button>
          </div>
        </div>
      </form>

      {incomingCount && pendingSnapshot ? (
        <button className="sticky top-[92px] z-20 mx-auto flex rounded-full border border-primary-100 bg-primary-700 px-4 py-2 text-xs font-semibold text-on-primary shadow-floating" onClick={() => commit(pendingSnapshot)} type="button">新增 {incomingCount} 条异动，点击更新</button>
      ) : null}

      {error ? <ErrorState message={error} onRetry={() => load(appliedFilters, { refresh: true })} retainedData={snapshot.loadedAt.getTime() > 0} /> : null}
      {marketError ? <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">{marketError}</div> : null}

      {!error ? (
        <div className="radar-cockpit-grid grid min-w-0 gap-3 2xl:h-[calc(100vh-11.5rem)]">
          <section className="cockpit-panel order-2 flex min-h-[520px] min-w-0 flex-col lg:order-2 2xl:order-1 2xl:min-h-0">
            <div className="cockpit-panel-header shrink-0">
              <div><h2 className="text-xs font-semibold text-text-primary">异动监控</h2><p className="mt-0.5 text-[10px] text-text-muted">公开事件 · 最新优先</p></div>
              <span className="inline-flex items-center gap-1.5 text-[10px] font-semibold text-text-secondary"><span className={`h-1.5 w-1.5 rounded-full ${loading || refreshing || streamState === "connecting" ? "animate-pulse bg-warn" : paused ? "bg-text-muted" : streamState === "reconnecting" ? "animate-pulse bg-amber-500" : "animate-pulse bg-good"}`} />{paused ? "PAUSED" : streamState === "reconnecting" ? "RECONNECTING" : streamState === "connecting" ? "CONNECTING" : "LIVE"}</span>
            </div>
            <div className="cockpit-scroll min-h-0 flex-1 overflow-y-auto">
              {initialLoading ? Array.from({ length: 7 }).map((_, index) => <div className="animate-pulse border-b border-border-subtle p-3" key={index}><div className="h-4 w-24 rounded bg-surface-container" /><div className="mt-3 h-3 w-full rounded bg-surface-container-low" /><div className="mt-2 h-3 w-3/4 rounded bg-surface-container-low" /></div>) : snapshot.signals.map((item) => <EventRow item={item} key={item.public_ref || item.id || `${item.symbol}-${item.time}`} onOpen={selectSignal} />)}
              {!loading && !snapshot.signals.length ? <div className="px-4 py-12 text-center"><div className="text-sm font-semibold text-text-primary">当前条件没有异动</div><p className="mt-2 text-xs leading-5 text-text-muted">清除币种或模块筛选，或切换更长时间窗口。</p><button className="btn-secondary mt-4 h-9 text-xs" onClick={reset} type="button">清除筛选</button></div> : null}
            </div>
          </section>

          <section aria-label="热钱观察与机会看板" className="order-3 min-w-0 space-y-3 lg:order-3 2xl:order-2 2xl:min-h-0 2xl:overflow-y-auto 2xl:pr-0.5">
            <section className="cockpit-panel">
              <div className="cockpit-panel-header">
                <div><h2 className="text-xs font-semibold text-text-primary">热钱观察榜单</h2><p className="mt-0.5 text-[10px] text-text-muted">历史榜单、实时 CVD、Surge 与短周期潜伏并列；缺失来源不会被模拟值替代</p></div>
                <span className="rounded bg-surface-container px-2 py-1 text-[10px] font-semibold text-text-secondary">{optionLabel(marketWindows, appliedFilters.window_sec)}</span>
              </div>
              <div className="grid gap-2.5 p-2.5 xl:grid-cols-2">
                {initialLoading ? Array.from({ length: 4 }).map((_, index) => <LoadingBoard key={index} />) : marketBoards.map((board) => <MarketBoardCard board={board} key={board.key} onSymbol={filterSymbol} />)}
                {!initialLoading && !marketBoards.length ? <div className="col-span-full rounded-md border border-dashed border-border-subtle px-4 py-12 text-center text-sm text-text-muted">市场榜单正在积累快照。信号事件区仍保持可用。</div> : null}
              </div>
            </section>
            <OpportunityList boards={snapshot.intelligence.boards || []} onOpen={selectSignal} />
          </section>

          <aside className="radar-overview-column order-1 min-w-0 lg:order-1 2xl:order-3 2xl:min-h-0 2xl:overflow-y-auto">
            <MarketStatePanel overview={snapshot.overview} />
            <TendencyPanel boards={snapshot.boards.boards || []} onSymbol={filterSymbol} />
            <section className="cockpit-panel">
              <div className="cockpit-panel-header"><div><h2 className="text-xs font-semibold text-text-primary">数据覆盖</h2><p className="mt-0.5 text-[10px] text-text-muted">缺失项保持为空，不按 0 参与判断</p></div><DataStatusBadge status={snapshot.overview.data_status} /></div>
              <div className="border-b border-border-subtle px-3 py-3">
                <div className="flex items-center justify-between text-[10px] text-text-muted"><span>30 天历史预热</span><span className="table-number font-semibold text-text-secondary">{warmupProgress.toFixed(1)}%</span></div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-container"><div className="h-full rounded-full bg-primary-600 transition-[width]" style={{ width: `${warmupProgress}%` }} /></div>
                <div className="mt-2 flex items-center justify-between text-[10px] text-text-muted"><span>已积累 {durationText(readiness?.history_span_sec)}</span><span>{readiness?.warmup_remaining_sec ? `约 ${durationText(readiness.warmup_remaining_sec)}后完整` : "目标已完成"}</span></div>
              </div>
              <MetricLine label="价格" value={`${compact(snapshot.overview.coverage?.price || 0)} / ${compact(snapshot.overview.coverage?.assets || 0)}`} />
              <MetricLine label="OI" value={`${compact(snapshot.overview.coverage?.oi || 0)} / ${compact(snapshot.overview.coverage?.assets || 0)}`} />
              <MetricLine label="现货主动资金" value={`${compact(snapshot.overview.coverage?.spot_flow || 0)} / ${compact(snapshot.overview.coverage?.assets || 0)}`} />
              <MetricLine label="合约主动资金" value={`${compact(snapshot.overview.coverage?.futures_flow || 0)} / ${compact(snapshot.overview.coverage?.assets || 0)}`} />
              {(snapshot.overview.warnings || []).length ? <div aria-live="polite" className="space-y-1.5 border-t border-border-subtle bg-amber-50/40 p-3" role="status">{snapshot.overview.warnings?.map((warning) => <p className="text-[10px] leading-4 text-amber-800" key={warning}>• {warning}</p>)}</div> : null}
            </section>
          </aside>
        </div>
      ) : null}

      {selectedSignalId ? <SignalDetailDrawer signalId={selectedSignalId} onClose={closeSignal} onSelectSignal={selectSignal} /> : null}
    </div>
  );
}

export default function RadarPage() {
  return cockpitV2Enabled ? <CockpitRadarPage /> : <LegacySignalRadar />;
}
