"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { DataStatusBadge } from "@/components/DataStatusBadge";
import { ErrorState } from "@/components/ErrorState";
import { FeatureUnavailable } from "@/components/FeatureUnavailable";
import { InlineEvidenceText } from "@/components/InlineEvidenceText";
import { PageTitle } from "@/components/PageTitle";
import { WatchlistButton } from "@/components/WatchlistButton";
import { getAgentsOverview } from "@/lib/api";
import { formatDateTime, formatMetricValue, safeText } from "@/lib/format";
import { cockpitV2Enabled } from "@/lib/features";
import type { AgentInsight, AgentsOverviewPayload, EvidenceFact } from "@/lib/types";

function statusTone(status?: string): "good" | "warn" | "bad" | "neutral" {
  if (status === "ready") return "good";
  if (status === "degraded") return "warn";
  if (status === "unavailable") return "bad";
  return "neutral";
}

function stateStyle(state?: string) {
  if (state === "strengthening") return "border-emerald-200 bg-emerald-50 text-emerald-700";
  if (state === "weakening" || state === "crowded") return "border-red-200 bg-red-50 text-red-700";
  if (state === "insufficient_data") return "border-amber-200 bg-amber-50 text-amber-700";
  return "border-primary-100 bg-primary-50 text-primary-700";
}

function EvidenceList({ refs = [], evidence }: { refs?: string[]; evidence: Map<string, EvidenceFact> }) {
  const items = refs.map((ref) => evidence.get(ref)).filter(Boolean) as EvidenceFact[];
  if (!items.length) return <p className="mt-2 text-[11px] text-text-muted">没有可展开的 ready 证据。</p>;
  return (
    <div className="mt-3 space-y-2">
      {items.map((item) => {
        const rendered = typeof item.value === "number" ? formatMetricValue(item.value, item.unit) : safeText(item.value);
        const content = <div className="rounded-lg border border-border-subtle bg-surface-panel p-2.5" key={item.ref}>
          <div className="flex items-center justify-between gap-3"><span className="text-[11px] font-semibold text-text-primary">{safeText(item.label)}</span><DataStatusBadge label={safeText(item.data_status).toUpperCase()} tone={statusTone(item.data_status)} /></div>
          <div className="table-number mt-1 break-words text-xs font-semibold text-text-secondary">{rendered}</div>
          <div className="mt-1 flex flex-wrap gap-x-2 text-[10px] text-text-muted"><span>{safeText(item.source)}</span><span>{formatDateTime(item.observed_at)}</span></div>
        </div>;
        return item.url ? <a className="block transition hover:border-primary-100" href={item.url} key={item.ref} rel={item.url.startsWith("http") ? "noopener noreferrer" : undefined} target={item.url.startsWith("http") ? "_blank" : undefined}>{content}</a> : content;
      })}
    </div>
  );
}

function InsightCard({ insight, evidence, compact = false }: { insight?: AgentInsight; evidence: Map<string, EvidenceFact>; compact?: boolean }) {
  if (!insight) return <div className="cockpit-panel p-4 text-xs text-text-muted">Agent 暂无结果。</div>;
  const confidence = typeof insight.confidence === "number" ? `${Math.round(insight.confidence * 100)}%` : "—";
  return (
    <article className="cockpit-panel min-w-0 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0"><h3 className="text-sm font-semibold text-text-primary">{safeText(insight.label, insight.scope)}</h3><p className="mt-1 text-[10px] text-text-muted">生成 {formatDateTime(insight.generated_at)} · 过期 {formatDateTime(insight.expires_at)}</p></div>
        <div className="flex flex-wrap items-center gap-1.5"><span className={`rounded-full border px-2 py-1 text-[10px] font-semibold ${stateStyle(insight.state)}`}>{safeText(insight.state_label, insight.state)}</span><DataStatusBadge label={safeText(insight.data_status).toUpperCase()} tone={statusTone(insight.data_status)} /></div>
      </div>
      <p className={`mt-4 text-sm leading-6 text-text-secondary ${compact ? "line-clamp-4" : ""}`}><InlineEvidenceText text={insight.summary} /></p>
      <div className="mt-3 flex flex-wrap gap-3 text-[10px] text-text-muted"><span>置信度 {confidence}</span><span>证据 {insight.evidence_refs?.length || 0}</span><span>反证 {insight.counter_evidence_refs?.length || 0}</span></div>
      {insight.missing_facts?.length ? <p className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-[11px] text-amber-800">缺失：{insight.missing_facts.join("、")}</p> : null}
      <details className="mt-4 rounded-lg border border-border-subtle bg-surface-container-low/40 p-3">
        <summary className="cursor-pointer text-xs font-semibold text-text-primary">展开证据来源</summary>
        <EvidenceList evidence={evidence} refs={insight.evidence_refs} />
        {insight.counter_evidence_refs?.length ? <div className="mt-3 border-t border-border-subtle pt-3"><div className="text-[10px] font-semibold uppercase tracking-wide text-red-600">反向证据 / 风险</div><EvidenceList evidence={evidence} refs={insight.counter_evidence_refs} /></div> : null}
      </details>
      {insight.actions ? <div className="mt-4 flex flex-wrap gap-2 border-t border-border-subtle pt-3">
        {insight.scope?.endsWith("USDT") ? <WatchlistButton compact symbol={insight.scope} /> : null}
        {insight.actions.coin_url ? <Link className="btn-secondary h-9 px-3 text-xs" href={insight.actions.coin_url}>单币证据</Link> : null}
        {insight.actions.radar_url ? <Link className="btn-secondary h-9 px-3 text-xs" href={insight.actions.radar_url}>雷达事件</Link> : null}
        {insight.actions.ai_url ? <a className="btn h-9 px-3 text-xs" href={insight.actions.ai_url} rel="noopener noreferrer" target="_blank">Telegram 追问 ↗</a> : null}
        {insight.actions.info_url ? <Link className="btn-secondary h-9 px-3 text-xs" href={insight.actions.info_url}>信息事件</Link> : null}
        {insight.actions.source_url ? <a className="btn-secondary h-9 px-3 text-xs" href={insight.actions.source_url} rel="noopener noreferrer" target="_blank">官方原文 ↗</a> : null}
      </div> : null}
    </article>
  );
}

function AgentsPageContent() {
  const [payload, setPayload] = useState<AgentsOverviewPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [windowSec, setWindowSec] = useState(14_400);
  const requestRef = useRef(0);

  async function load(refresh = false) {
    const request = ++requestRef.current;
    if (!refresh) setPayload(null);
    setLoading(true);
    setError("");
    try {
      const next = await getAgentsOverview(windowSec, { bypassCache: refresh });
      if (request === requestRef.current) setPayload(next);
    } catch (loadError) {
      if (request === requestRef.current) setError(loadError instanceof Error ? loadError.message : "AI 决策加载失败");
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  }

  useEffect(() => { void load(); }, [windowSec]);
  const evidence = useMemo(() => new Map((payload?.evidence || []).map((item) => [safeText(item.ref), item])), [payload?.evidence]);
  const anomalies = payload?.agents?.anomalies || [];
  const messages = payload?.agents?.messages || [];

  return (
    <div aria-busy={loading} className="space-y-3">
      <PageTitle title="AI 决策" subtitle="规则先生成状态，AI 只负责压缩表达；每条结论都能展开到时间、来源和状态明确的证据。" tags={[`引擎 ${safeText(payload?.engine_version)}`, "Evidence First", "不构成投资建议"]} />

      <section className="cockpit-panel p-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div aria-label="决策时间窗口" className="flex flex-wrap gap-1 rounded-lg bg-surface-container-low p-1" role="group">{[
            { value: 3600, label: "1h" }, { value: 14_400, label: "4h" }, { value: 86_400, label: "1d" }
          ].map((item) => <button aria-pressed={windowSec === item.value} className={`h-8 rounded-md px-4 text-xs font-semibold ${windowSec === item.value ? "bg-surface-panel text-primary-700 shadow-soft" : "text-text-secondary"}`} key={item.value} onClick={() => setWindowSec(item.value)} type="button">{item.label}</button>)}</div>
          <div className="flex items-center gap-2"><DataStatusBadge label={safeText(payload?.data_status, loading ? "loading" : "empty").toUpperCase()} tone={statusTone(payload?.data_status)} /><button className="btn-secondary h-9 px-3 text-xs" disabled={loading} onClick={() => void load(true)} type="button">{loading ? "刷新中" : "刷新结论"}</button></div>
        </div>
      </section>

      {error ? <ErrorState message={error} onRetry={() => void load(true)} retainedData={Boolean(payload)} /> : null}

      <section className="grid grid-cols-2 gap-2 lg:grid-cols-5">
        {[
          ["结构化结论", payload ? payload.coverage?.insights || 0 : "—"], ["Ready", payload ? payload.coverage?.ready || 0 : "—"], ["证据引用", payload ? payload.coverage?.evidence || 0 : "—"], ["雷达信号", payload ? payload.coverage?.signals || 0 : "—"], ["资讯输入", payload ? payload.coverage?.news_events || 0 : "—"]
        ].map(([label, value]) => <div className="cockpit-panel p-3" key={String(label)}><div className="text-[10px] font-semibold text-text-muted">{label}</div><div className="table-number mt-1 text-xl font-semibold text-text-primary">{value}</div></div>)}
      </section>

      <section>
        <div className="mb-2 flex items-end justify-between"><div><h2 className="text-sm font-semibold text-text-primary">全局 Agent</h2><p className="mt-0.5 text-[11px] text-text-muted">市场阶段、广度与现货/合约主动资金联合判断</p></div><span className="text-[10px] text-text-muted">{formatDateTime(payload?.generated_at)}</span></div>
        {loading && !payload ? <div className="cockpit-panel h-60 animate-pulse bg-surface-container-low" /> : <InsightCard evidence={evidence} insight={payload?.agents?.global} />}
      </section>

      <section>
        <div className="mb-2"><h2 className="text-sm font-semibold text-text-primary">BTC / ETH 解盘 Agent</h2><p className="mt-0.5 text-[11px] text-text-muted">价格、OI、现货/合约资金与费率必须全部 ready 才生成方向状态</p></div>
        <div className="grid gap-3 lg:grid-cols-2">{(payload?.agents?.majors || []).map((insight) => <InsightCard evidence={evidence} insight={insight} key={insight.insight_id} />)}{loading && !payload ? Array.from({ length: 2 }).map((_, index) => <div className="cockpit-panel h-72 animate-pulse bg-surface-container-low" key={index} />) : null}</div>
      </section>

      <section>
        <div className="mb-2"><h2 className="text-sm font-semibold text-text-primary">异常候选 Agent</h2><p className="mt-0.5 text-[11px] text-text-muted">仅用于偏强、偏弱和风险观察，不提供直接下单语言</p></div>
        {anomalies.length ? <div className="grid gap-3 xl:grid-cols-3">{anomalies.map((insight) => <InsightCard compact evidence={evidence} insight={insight} key={insight.insight_id} />)}</div> : <div className="cockpit-panel px-4 py-14 text-center text-xs text-text-muted">当前窗口没有已发送的异常候选信号。</div>}
      </section>

      <section>
        <div className="mb-2 flex items-end justify-between"><div><h2 className="text-sm font-semibold text-text-primary">消息 Agent</h2><p className="mt-0.5 text-[11px] text-text-muted">只展示已索引的高重要度官方事件，原文事实与规则推断分离</p></div><Link className="text-xs font-semibold text-primary-700" href="/info">打开信息中心</Link></div>
        {messages.length ? <div className="grid gap-3 lg:grid-cols-2">{messages.map((insight) => <InsightCard compact evidence={evidence} insight={insight} key={insight.insight_id} />)}</div> : <div className="cockpit-panel px-4 py-14 text-center text-xs text-text-muted">最近 24h 没有已索引的高重要度官方公告。</div>}
      </section>

      <section className="cockpit-panel border-primary-100 bg-primary-50/50 p-4">
        <h2 className="text-xs font-semibold text-primary-900">Agent 安全门禁</h2>
        <div className="mt-3 grid gap-2 text-[11px] leading-5 text-primary-800 sm:grid-cols-2 lg:grid-cols-4"><p>✓ 结构化事实先于文字结论</p><p>✓ unavailable / degraded 不做肯定性判断</p><p>✓ 数字由代码格式化，不交给模型计算</p><p>✓ 结果保存版本、证据和过期时间</p></div>
        <p className="mt-3 border-t border-primary-100 pt-3 text-[11px] font-semibold text-primary-900">{safeText(payload?.safety?.disclaimer, "市场观察，不构成投资建议。")}</p>
      </section>

      {(payload?.warnings || []).length ? <section aria-live="polite" className="cockpit-panel border-amber-200 bg-amber-50/70 p-3" role="status"><h2 className="text-xs font-semibold text-amber-900">数据降级说明</h2><ul className="mt-2 text-[11px] leading-5 text-amber-800">{payload?.warnings?.map((warning) => <li key={warning}>· {warning}</li>)}</ul></section> : null}
    </div>
  );
}

export default function AgentsPage() {
  return cockpitV2Enabled ? <AgentsPageContent /> : <FeatureUnavailable title="AI 决策" />;
}
