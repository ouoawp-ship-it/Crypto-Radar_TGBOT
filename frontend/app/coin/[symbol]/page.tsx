"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { CandlestickChart } from "@/components/CandlestickChart";
import { DataStatusBadge } from "@/components/DataStatusBadge";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { MetricSeriesChart } from "@/components/MetricSeriesChart";
import { PageTitle } from "@/components/PageTitle";
import { SignalCard } from "@/components/SignalCard";
import { WatchlistButton } from "@/components/WatchlistButton";
import { getCoinContext } from "@/lib/api";
import { formatDateTime, formatMetricValue, freshnessLabel, safeText } from "@/lib/format";
import type { CoinContext, MarketMetric, SignalIntelligence } from "@/lib/types";
import { normalizeWatchSymbol } from "@/lib/watchlist";

const INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"];

function MetricCard({ label, metric }: { label: string; metric?: MarketMetric }) {
  return (
    <div className="rounded-lg border border-border-subtle bg-surface-panel p-3">
      <div className="text-[11px] font-semibold text-text-muted">{label}</div>
      <div className="table-number mt-1 text-lg font-semibold text-text-primary">{metric?.value == null ? "—" : formatMetricValue(metric.value, metric.unit)}</div>
      <div className="mt-1 truncate text-[10px] text-text-muted">{metric ? `${safeText(metric.source)} · ${freshnessLabel(metric.status, metric.age_sec)}` : "数据积累中"}</div>
    </div>
  );
}

function statusTone(status?: string): "good" | "warn" | "bad" | "neutral" {
  if (status === "ready" || status === "fresh") return "good";
  if (status === "degraded" || status === "stale") return "warn";
  if (status === "unavailable") return "bad";
  return "neutral";
}

function RankItem({ label, value }: { label: string; value?: { available?: boolean; percentile?: number; rank?: number; sample_size?: number; reason?: string } }) {
  return <div className="flex items-center justify-between gap-3 border-b border-border-subtle px-3 py-2.5 last:border-0"><span className="text-xs text-text-secondary">{label}</span><span className="table-number text-xs font-semibold text-text-primary">{value?.available ? `P${Math.round(Number(value.percentile || 0))} · #${value.rank}/${value.sample_size}` : safeText(value?.reason, "样本不足")}</span></div>;
}

export default function CoinContextPage() {
  const params = useParams<{ symbol: string }>();
  const symbol = useMemo(() => normalizeWatchSymbol(decodeURIComponent(String(params?.symbol || ""))), [params]);
  const [data, setData] = useState<CoinContext | null>(null);
  const [marketType, setMarketType] = useState<"spot" | "futures">("futures");
  const [interval, setInterval] = useState("15m");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [ready, setReady] = useState(false);
  const requestRef = useRef(0);

  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    const market = query.get("market_type");
    const selectedInterval = query.get("interval");
    if (market === "spot" || market === "futures") setMarketType(market);
    if (selectedInterval && INTERVALS.includes(selectedInterval)) setInterval(selectedInterval);
    setReady(true);
  }, []);

  useEffect(() => {
    if (!ready) return;
    const query = new URLSearchParams(window.location.search);
    query.set("market_type", marketType);
    query.set("interval", interval);
    window.history.replaceState({}, "", `${window.location.pathname}?${query.toString()}`);
  }, [ready, marketType, interval]);

  async function load(refresh = false) {
    if (!symbol) return;
    const request = ++requestRef.current;
    if (!refresh) setData(null);
    setLoading(true);
    setError("");
    try {
      const payload = await getCoinContext(symbol, { bypassCache: refresh }, { market_type: marketType, interval, bars: 96 });
      if (request === requestRef.current) setData(payload);
    } catch (loadError) {
      if (request === requestRef.current) setError(loadError instanceof Error ? loadError.message : "单币证据加载失败");
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  }

  useEffect(() => { if (ready) void load(); }, [ready, symbol, marketType, interval]);

  const metrics = data?.market?.metrics || {};
  const timeline = data?.timeline || [];
  const moduleCounts = Object.entries(data?.summary?.module_counts || {}).sort((a, b) => b[1] - a[1]);
  const latestIntelligence = timeline.find((item) => item.intelligence)?.intelligence as SignalIntelligence | undefined;
  const series = data?.series?.points || [];

  async function copyShareLink() {
    const url = new URL(data?.actions?.share_url || window.location.pathname, window.location.origin).toString();
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div aria-busy={loading} className="space-y-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <PageTitle title={`${data?.coin || symbol.replace("USDT", "")} 单币上下文`} subtitle="用行情、资金、OI、费率、排名与事件证据验证雷达信号，所有缺失值保持不可用。" tags={[safeText(data?.data_status, loading ? "LOADING" : "EMPTY").toUpperCase(), data ? `${data.evidence_coverage?.chart_points || 0} 根 K 线` : "K 线 —", data ? `${data.summary?.signal_count || 0} 条事件` : "事件 —"]} />
        <div className="mb-5 flex flex-wrap gap-2">
          <WatchlistButton symbol={symbol} />
          <Link className="btn-secondary" href={data?.actions?.radar_url || `/radar?symbol=${symbol}`}>只看该币信号</Link>
          {data?.actions?.ai_url ? <a className="btn-secondary" href={data.actions.ai_url} rel="noreferrer" target="_blank">交给 AI 分析</a> : null}
          {data?.actions?.alert_url ? <a className="btn" href={data.actions.alert_url} rel="noreferrer" target="_blank">设置提醒</a> : null}
          <button className="btn-secondary" onClick={() => void copyShareLink()} type="button">{copied ? "已复制" : "复制链接"}</button>
        </div>
      </div>

      {error ? <ErrorState message={error} onRetry={() => void load(true)} retainedData={Boolean(data)} /> : null}
      {loading && !data ? <div className="grid grid-cols-2 gap-2 md:grid-cols-4">{Array.from({ length: 8 }).map((_, index) => <div className="h-24 animate-pulse rounded-lg bg-surface-container" key={index} />)}</div> : null}

      {data ? (
        <>
          <section className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
            <MetricCard label="当前价格" metric={metrics.price} />
            <MetricCard label="24h 涨跌" metric={metrics.price_24h_pct} />
            <MetricCard label="24h 成交额" metric={metrics.quote_volume} />
            <MetricCard label="市值" metric={metrics.market_cap} />
            <MetricCard label="合约 OI" metric={metrics.oi_value} />
            <MetricCard label="1h 价格" metric={metrics.price_1h_pct} />
            <MetricCard label="1h OI" metric={metrics.oi_1h_pct} />
            <MetricCard label="资金费率" metric={metrics.funding_pct} />
          </section>

          <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_320px]">
            <div className="min-w-0 space-y-3">
              <section className="cockpit-panel">
                <div className="cockpit-panel-header flex-wrap">
                  <div><h2 className="text-sm font-semibold text-text-primary">K 线与成交量</h2><p className="mt-0.5 text-[11px] text-text-muted">{safeText(data.chart?.source, "数据源不可用")} · {data.chart?.coverage?.returned || 0}/{data.chart?.coverage?.requested || 0}</p></div>
                  <div className="flex flex-wrap items-center gap-1">
                    <div aria-label="行情类型" className="flex rounded-md bg-surface-container-low p-0.5" role="group">{(["spot", "futures"] as const).map((item) => <button aria-pressed={marketType === item} className={`h-7 rounded px-2.5 text-[11px] font-semibold ${marketType === item ? "bg-surface-panel text-primary-700 shadow-soft" : "text-text-secondary"}`} key={item} onClick={() => setMarketType(item)} type="button">{item === "spot" ? "现货" : "合约"}</button>)}</div>
                    <div aria-label="K 线周期" className="flex max-w-full overflow-x-auto rounded-md bg-surface-container-low p-0.5" role="group">{INTERVALS.map((item) => <button aria-pressed={interval === item} className={`h-7 rounded px-2 text-[11px] font-semibold ${interval === item ? "bg-surface-panel text-primary-700 shadow-soft" : "text-text-secondary"}`} key={item} onClick={() => setInterval(item)} type="button">{item}</button>)}</div>
                    <button className="btn-secondary h-8 px-3 text-[11px]" disabled={loading} onClick={() => void load(true)} type="button">{loading ? "刷新中" : "刷新"}</button>
                  </div>
                </div>
                <div className="p-2"><CandlestickChart points={data.chart?.points || []} /></div>
              </section>

              <section className="cockpit-panel">
                <div className="cockpit-panel-header"><div><h2 className="text-sm font-semibold text-text-primary">快照证据曲线</h2><p className="mt-0.5 text-[11px] text-text-muted">服务端快照原样本 · 不在前端插值</p></div><DataStatusBadge label={safeText(data.series?.data_status).toUpperCase()} tone={statusTone(data.series?.data_status)} /></div>
                <div className="grid gap-px bg-border-subtle sm:grid-cols-2 xl:grid-cols-4">
                  <div className="bg-surface-panel p-3"><MetricSeriesChart label="价格" metric="price" points={series} /></div>
                  <div className="bg-surface-panel p-3"><MetricSeriesChart label="OI" metric="oi_usd" points={series} unit="usd" /></div>
                  <div className="bg-surface-panel p-3"><MetricSeriesChart label="现货 CVD" metric="spot_flow_usd" points={series} unit="usd" /></div>
                  <div className="bg-surface-panel p-3"><MetricSeriesChart label="合约 CVD" metric="futures_flow_usd" points={series} unit="usd" /></div>
                </div>
              </section>

              <section className="cockpit-panel">
                <div className="cockpit-panel-header"><div><h2 className="text-sm font-semibold text-text-primary">信号时间线</h2><p className="mt-0.5 text-[11px] text-text-muted">按时间倒序查看生命周期、排名、共振和公开摘要</p></div><span className="table-number text-sm font-semibold text-text-primary">{data.summary?.signal_count || 0}</span></div>
                {timeline.length ? <div className="grid gap-3 p-3 lg:grid-cols-2">{timeline.map((item) => <SignalCard context="default" item={item} key={item.public_ref || item.id} />)}</div> : <div className="p-4"><EmptyState title="该币暂无信号" text="行情快照仍可查看；雷达产生可信信号后会自动进入时间线。" /></div>}
              </section>
            </div>

            <aside className="space-y-3">
              <section className="cockpit-panel">
                <div className="cockpit-panel-header"><div><h2 className="text-sm font-semibold text-text-primary">证据覆盖</h2><p className="mt-0.5 text-[11px] text-text-muted">{formatDateTime(data.market?.updated_at)} · {freshnessLabel(data.market?.status, data.market?.age_sec)}</p></div><DataStatusBadge label={safeText(data.data_status).toUpperCase()} tone={statusTone(data.data_status)} /></div>
                {[["市场快照", data.evidence_coverage?.market], ["K 线样本", data.evidence_coverage?.chart_points], ["历史快照", data.evidence_coverage?.snapshot_points], ["历史信号", data.evidence_coverage?.signals], ["关联资讯", data.evidence_coverage?.related_info], ["官方公告", data.evidence_coverage?.announcements]].map(([label, value]) => <div className="flex items-center justify-between border-b border-border-subtle px-3 py-2.5 last:border-0" key={String(label)}><span className="text-xs text-text-secondary">{label}</span><span className="table-number text-xs font-semibold text-text-primary">{Number(value || 0)}</span></div>)}
              </section>

              <section className="cockpit-panel">
                <div className="cockpit-panel-header"><div><h2 className="text-sm font-semibold text-text-primary">排名与生命周期</h2><p className="mt-0.5 text-[11px] text-text-muted">来自最近可用雷达事件</p></div></div>
                <RankItem label="自身历史" value={latestIntelligence?.self_rank} />
                <RankItem label="市场强度" value={latestIntelligence?.market_strength_rank} />
                <RankItem label="绝对量级" value={latestIntelligence?.market_absolute_rank} />
                <div className="border-t border-border-subtle p-3"><div className="text-xs font-semibold text-text-primary">{safeText(latestIntelligence?.lifecycle?.label, "尚无生命周期")}</div><p className="mt-1 text-[11px] leading-5 text-text-muted">{safeText(latestIntelligence?.lifecycle?.basis, "需要至少一条可用信号后才能判断。")}</p></div>
              </section>

              <section className="cockpit-panel">
                <div className="cockpit-panel-header"><div><h2 className="text-sm font-semibold text-text-primary">多交易所费率</h2><p className="mt-0.5 text-[11px] text-text-muted">不同结算周期不直接混排</p></div></div>
                {(data.market?.funding_exchanges || []).length ? (data.market?.funding_exchanges || []).map((row, index) => <div className="flex items-center justify-between gap-3 border-b border-border-subtle px-3 py-2.5 last:border-0" key={`${row.exchange}-${index}`}><div><div className="text-xs font-semibold text-text-primary">{safeText(row.exchange)}</div><div className="mt-0.5 text-[10px] text-text-muted">{row.interval_hours ? `${row.interval_hours}H` : "周期未知"}{row.next_funding_time ? ` · ${row.next_funding_time}` : ""}</div></div><span className={`table-number text-xs font-semibold ${Number(row.funding_pct || 0) >= 0 ? "text-emerald-700" : "text-red-700"}`}>{formatMetricValue(row.funding_pct, "percent_per_cycle")}</span></div>) : <div className="px-3 py-8 text-center text-xs text-text-muted">多交易所费率暂时不可用</div>}
              </section>

              <section className="cockpit-panel">
                <div className="cockpit-panel-header"><div><h2 className="text-sm font-semibold text-text-primary">关联资讯</h2><p className="mt-0.5 text-[11px] text-text-muted">公开资讯索引与统一信号库公告</p></div></div>
                {(data.related_info?.items || []).length ? (data.related_info?.items || []).map((item, index) => <a className="block border-b border-border-subtle px-3 py-2.5 last:border-0 hover:bg-surface-container-low" href={item.url || `/radar?symbol=${symbol}&signal=${item.public_ref || item.id || ""}`} key={item.event_id || item.public_ref || item.id || index} rel={item.url ? "noreferrer" : undefined} target={item.url ? "_blank" : undefined}><div className="text-xs font-semibold text-text-primary">{safeText(item.title || item.display?.title, item.summary || item.excerpt)}</div><div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-text-muted"><span className="truncate">{safeText(item.source, item.display?.module_label)}</span><span className="shrink-0">{formatDateTime(item.published_at || item.time)}</span></div></a>) : <div className="px-3 py-8 text-center text-xs text-text-muted">暂无已验证的关联资讯</div>}
              </section>

              {moduleCounts.length ? <section className="cockpit-panel p-3"><h2 className="text-xs font-semibold text-text-primary">信号模块分布</h2><div className="mt-2 flex flex-wrap gap-1.5">{moduleCounts.map(([module, count]) => <span className="chip" key={module}>{module} · {count}</span>)}</div></section> : null}
            </aside>
          </div>

          {(data.warnings || []).length ? <section aria-live="polite" className="cockpit-panel border-amber-200 bg-amber-50/70 p-3" role="status"><h2 className="text-xs font-semibold text-amber-900">数据降级说明</h2><ul className="mt-2 space-y-1 text-[11px] leading-5 text-amber-800">{(data.warnings || []).map((item) => <li key={item}>· {item}</li>)}</ul></section> : null}
        </>
      ) : null}
    </div>
  );
}
