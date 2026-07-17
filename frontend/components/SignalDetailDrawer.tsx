"use client";

import { useEffect, useRef, useState } from "react";
import { getSignalContext } from "@/lib/api";
import { formatDateTime, formatMetricValue, freshnessLabel, safeText } from "@/lib/format";
import type { MarketMetric, SignalContext, Tone } from "@/lib/types";
import { DataStatusBadge } from "./DataStatusBadge";
import { WatchlistButton } from "./WatchlistButton";

function statusTone(status?: string): Tone {
  if (status === "fresh") return "good";
  if (status === "degraded" || status === "stale") return "warn";
  if (status === "unavailable") return "bad";
  return "neutral";
}

function metricTone(metric?: MarketMetric): string {
  const value = Number(metric?.value);
  if (!Number.isFinite(value) || value === 0) return "text-text-primary";
  if (metric?.unit === "percent" || metric?.unit === "percent_per_cycle") {
    return value > 0 ? "text-good" : "text-risk";
  }
  return "text-text-primary";
}

function DrawerSkeleton() {
  return (
    <div className="space-y-4 p-5" aria-hidden="true">
      <div className="h-24 animate-pulse rounded-xl bg-surface-container-low" />
      <div className="grid grid-cols-2 gap-3">
        {Array.from({ length: 6 }).map((_, index) => <div className="h-24 animate-pulse rounded-xl bg-surface-container-low" key={index} />)}
      </div>
      <div className="h-48 animate-pulse rounded-xl bg-surface-container-low" />
    </div>
  );
}

export function SignalDetailDrawer({
  signalId,
  onClose,
  onSelectSignal
}: {
  signalId: number | string;
  onClose: () => void;
  onSelectSignal?: (signalId: number | string) => void;
}) {
  const [context, setContext] = useState<SignalContext | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const requestRef = useRef(0);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  async function load(refresh = false) {
    const request = ++requestRef.current;
    setLoading(true);
    setError("");
    try {
      const next = await getSignalContext(signalId, { bypassCache: refresh });
      if (request === requestRef.current) setContext(next);
    } catch (loadError) {
      if (request === requestRef.current) setError(loadError instanceof Error ? loadError.message : "信号上下文加载失败");
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  }

  useEffect(() => {
    setContext(null);
    void load();
    return () => {
      requestRef.current += 1;
    };
  }, [signalId]);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'
      )).filter((element) => !element.hasAttribute("hidden") && element.getAttribute("aria-hidden") !== "true");
      if (!focusable.length) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", handleKey);
      if (previousFocusRef.current?.isConnected) previousFocusRef.current.focus();
    };
  }, []);

  const signal = context?.signal;
  const market = context?.market;
  const display = signal?.display || {};
  const fundingRows = market?.funding_exchanges || [];
  const related = context?.related?.same_symbol || [];

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-slate-950/35 backdrop-blur-[2px]" onMouseDown={onClose}>
      <aside
        ref={dialogRef}
        aria-label="信号上下文详情"
        aria-modal="true"
        className="h-full w-full overflow-y-auto bg-surface-panel shadow-2xl sm:max-w-[540px]"
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border-subtle bg-surface-panel/95 px-4 py-3 backdrop-blur sm:px-5">
          <div>
            <div className="text-xs font-semibold tracking-[0.08em] text-text-muted">SIGNAL CONTEXT</div>
            <div className="mt-0.5 text-sm font-semibold text-text-primary">证据与市场上下文</div>
          </div>
          <button ref={closeRef} aria-label="关闭信号详情" className="grid h-11 w-11 place-items-center rounded-lg border border-border-subtle text-xl text-text-secondary transition hover:bg-surface-canvas" onClick={onClose}>
            ×
          </button>
        </div>

        {loading && !context ? <DrawerSkeleton /> : null}

        {error ? (
          <div className="m-5 rounded-xl border border-risk/25 bg-risk/5 p-5">
            <div className="font-semibold text-risk">上下文加载失败</div>
            <p className="mt-2 text-sm text-risk">{error}</p>
            <button className="btn mt-4" onClick={() => void load(true)}>重新加载</button>
          </div>
        ) : null}

        {context ? (
          <div className="space-y-5 p-4 pb-24 sm:p-5 sm:pb-10">
            <section className="rounded-xl border border-border-subtle bg-surface-bright p-5">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="table-number text-2xl font-semibold text-text-primary">{safeText(signal?.symbol, "全局信号")}</div>
                  <div className="mt-1 text-xs text-text-muted">{formatDateTime(signal?.time)} · 信号 #{signal?.id}</div>
                </div>
                <DataStatusBadge label={context.lifecycle?.label || "已记录"} tone="info" />
              </div>
              <h2 className="mt-5 text-base font-semibold leading-6 text-text-primary">{safeText(display.title || signal?.signal_type, "市场信号")}</h2>
              <p className="mt-2 text-sm leading-6 text-text-secondary">{safeText(display.summary || signal?.excerpt, "暂无公开摘要。")}</p>
              <div className="mt-4 flex flex-wrap gap-2">
                <DataStatusBadge label={safeText(display.module_label, signal?.module || "其他")} tone="info" />
                {market ? <DataStatusBadge label={freshnessLabel(market.status, market.age_sec)} tone={statusTone(market.status)} /> : null}
              </div>
            </section>

            <section>
              <div className="mb-3">
                <h3 className="section-title">相对排名与生命周期</h3>
                <p className="mt-1 text-xs text-text-muted">只使用同口径可验证样本；样本不足时不会伪造排名。</p>
              </div>
              <div className="grid grid-cols-3 gap-2">
                {[
                  ["自身极端度", context.rankings?.self],
                  ["市场强度", context.rankings?.market_strength],
                  ["绝对规模", context.rankings?.market_absolute]
                ].map(([label, rawRank]) => {
                  const rank = rawRank as NonNullable<typeof context.rankings>["self"];
                  return (
                    <div className="rounded-xl border border-border-subtle bg-white p-3" key={String(label)} title={rank?.method || rank?.reason}>
                      <div className="text-[11px] font-semibold text-text-muted">{String(label)}</div>
                      <div className="table-number mt-2 text-base font-semibold text-text-primary">{rank?.available ? `P${Math.round(Number(rank.percentile || 0))}` : "—"}</div>
                      <div className="mt-1 text-[10px] text-text-muted">{rank?.available ? `#${rank.rank}/${rank.sample_size}` : "样本积累中"}</div>
                    </div>
                  );
                })}
              </div>
              {context.resonance?.windows?.length ? (
                <div className="mt-3 rounded-xl border border-border-subtle p-3">
                  <div className="flex gap-1.5">
                    {context.resonance.windows.map((window) => (
                      <span className={`flex-1 rounded-md py-2 text-center text-[11px] font-semibold ${window.active ? "bg-primary-600 text-on-primary" : "bg-surface-container text-text-muted"}`} key={window.key}>
                        {window.key}
                      </span>
                    ))}
                  </div>
                  <p className="mt-2 text-[11px] leading-4 text-text-muted">{context.resonance.method}</p>
                </div>
              ) : null}
              {context.lifecycle?.basis ? <p className="mt-2 text-xs leading-5 text-text-muted">状态依据：{context.lifecycle.basis}</p> : null}
            </section>

            <section>
              <div className="mb-3 flex items-end justify-between gap-3">
                <div>
                  <h3 className="section-title">关键信号证据</h3>
                  <p className="mt-1 text-xs text-text-muted">每个数据都带来源、新鲜度与单位。</p>
                </div>
                {market?.updated_at ? <span className="text-xs text-text-muted">{formatDateTime(market.updated_at)}</span> : null}
              </div>
              {context.evidence?.length ? (
                <div className="grid grid-cols-2 gap-3">
                  {context.evidence.map((evidence) => (
                    <div className="rounded-xl border border-border-subtle bg-white p-4" key={evidence.key}>
                      <div className="text-xs font-semibold text-text-muted">{safeText(evidence.label)}</div>
                      <div className={`table-number mt-2 text-lg font-semibold ${metricTone(evidence.metric)}`}>
                        {evidence.metric ? formatMetricValue(evidence.metric.value, evidence.metric.unit) : safeText(evidence.value)}
                      </div>
                      <div className="mt-1 text-[11px] leading-4 text-text-muted">
                        {evidence.metric ? `${safeText(evidence.metric.source)} · ${freshnessLabel(evidence.metric.status, evidence.metric.age_sec)}` : safeText(evidence.description)}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="rounded-xl border border-dashed border-border-subtle p-5 text-sm text-text-muted">市场证据暂时不可用，信号原始记录仍可查看。</div>
              )}
              {context.market_error ? <p className="mt-2 text-xs text-warn">{context.market_error}</p> : null}
            </section>

            {fundingRows.length ? (
              <section>
                <h3 className="section-title">多交易所资金费率</h3>
                <div className="mt-3 overflow-hidden rounded-xl border border-border-subtle">
                  {fundingRows.map((row, index) => (
                    <div className={`grid grid-cols-[1fr_auto] gap-4 px-4 py-3 text-sm ${index ? "border-t border-border-subtle" : ""}`} key={`${row.exchange}-${index}`}>
                      <div>
                        <div className="font-semibold text-text-primary">{safeText(row.exchange)}</div>
                        <div className="mt-1 text-xs text-text-muted">{row.interval_hours ? `${row.interval_hours}H 周期` : "结算周期未知"}{row.next_funding_time ? ` · 下次 ${row.next_funding_time}` : ""}</div>
                      </div>
                      <div className={`table-number self-center font-semibold ${Number(row.funding_pct) >= 0 ? "text-good" : "text-risk"}`}>
                        {formatMetricValue(row.funding_pct, "percent_per_cycle")}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            ) : null}

            {related.length ? (
              <section>
                <h3 className="section-title">同币种最近信号</h3>
                <div className="mt-3 space-y-2">
                  {related.map((item) => (
                    <button className="flex w-full items-center justify-between gap-4 rounded-xl border border-border-subtle p-3 text-left transition hover:border-primary-100 hover:bg-surface-bright" key={item.public_ref || item.id} onClick={() => {
                      const reference = item.public_ref || item.id;
                      if (reference) onSelectSignal?.(reference);
                    }}>
                      <span className="min-w-0"><span className="block truncate text-sm font-semibold text-text-primary">{safeText(item.display?.title || item.signal_type)}</span><span className="mt-1 block text-xs text-text-muted">{formatDateTime(item.time)}</span></span>
                      <span className="shrink-0 text-primary-700">→</span>
                    </button>
                  ))}
                </div>
              </section>
            ) : null}

            <section className="grid grid-cols-2 gap-3 border-t border-border-subtle pt-5">
              <a className="btn-secondary" href={context.actions?.symbol_url || "/radar"}>只看该币</a>
              {signal?.symbol ? <a className="btn-secondary" href={`/coin/${signal.symbol}`}>单币上下文</a> : null}
              {signal?.symbol ? <WatchlistButton compact symbol={signal.symbol} /> : null}
              <button className="btn" onClick={() => void load(true)}>刷新上下文</button>
              {context.actions?.ai_url ? <a className="btn-secondary" href={context.actions.ai_url} rel="noreferrer" target="_blank">交给 AI 分析</a> : null}
              {context.actions?.alert_url ? <a className="btn" href={context.actions.alert_url} rel="noreferrer" target="_blank">设置个性化提醒</a> : null}
            </section>
          </div>
        ) : null}
      </aside>
    </div>
  );
}
