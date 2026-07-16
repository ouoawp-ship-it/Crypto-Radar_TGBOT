"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { PageTitle } from "@/components/PageTitle";
import { SignalCard } from "@/components/SignalCard";
import { WatchlistButton } from "@/components/WatchlistButton";
import { getCoinContext } from "@/lib/api";
import { formatDateTime, formatMetricValue, freshnessLabel, safeText } from "@/lib/format";
import type { CoinContext, MarketMetric } from "@/lib/types";
import { normalizeWatchSymbol } from "@/lib/watchlist";

function MetricCard({ label, metric }: { label: string; metric?: MarketMetric }) {
  return (
    <div className="rounded-xl border border-border-subtle bg-white p-4">
      <div className="text-xs font-semibold text-text-muted">{label}</div>
      <div className="table-number mt-2 text-xl font-semibold text-text-primary">{metric?.value == null ? "—" : formatMetricValue(metric.value, metric.unit)}</div>
      <div className="mt-1 text-[11px] text-text-muted">{metric ? `${safeText(metric.source)} · ${freshnessLabel(metric.status, metric.age_sec)}` : "数据积累中"}</div>
    </div>
  );
}

export default function CoinContextPage() {
  const params = useParams<{ symbol: string }>();
  const symbol = useMemo(() => normalizeWatchSymbol(decodeURIComponent(String(params?.symbol || ""))), [params]);
  const [data, setData] = useState<CoinContext | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  async function load(refresh = false) {
    if (!symbol) return;
    setLoading(true);
    setError("");
    try {
      setData(await getCoinContext(symbol, { bypassCache: refresh }));
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "单币上下文加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, [symbol]);

  const metrics = data?.market?.metrics || {};
  const timeline = data?.timeline || [];
  const moduleCounts = Object.entries(data?.summary?.module_counts || {}).sort((a, b) => b[1] - a[1]);

  return (
    <div className="space-y-5">
      <PageTitle title={`${data?.coin || symbol.replace("USDT", "")} 单币上下文`} subtitle="聚合当前行情、信号证据和历史时间线，用于验证雷达信号；不扩展为研究或交易执行平台。" tags={["服务端聚合", "30 天信号", "只读验证"]} />

      <div className="flex flex-wrap gap-2">
        <WatchlistButton symbol={symbol} />
        <Link className="btn-secondary" href={data?.actions?.radar_url || `/radar?symbol=${symbol}`}>只看该币信号</Link>
        {data?.actions?.ai_url ? <a className="btn-secondary" href={data.actions.ai_url} rel="noreferrer" target="_blank">交给 AI 分析</a> : null}
        {data?.actions?.alert_url ? <a className="btn" href={data.actions.alert_url} rel="noreferrer" target="_blank">设置提醒</a> : null}
      </div>

      {error ? <ErrorState message={error} onRetry={() => void load(true)} /> : null}

      {loading && !data ? (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">{Array.from({ length: 8 }).map((_, index) => <div className="h-28 animate-pulse rounded-xl bg-surface-container" key={index} />)}</div>
      ) : null}

      {data ? (
        <>
          <section className="panel overflow-hidden">
            <div className="flex flex-col gap-3 border-b border-border-subtle px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
              <div><h2 className="section-title">市场快照</h2><p className="mt-1 text-xs text-text-muted">更新于 {formatDateTime(data.market?.updated_at)} · {freshnessLabel(data.market?.status, data.market?.age_sec)}</p></div>
              <span className="chip">{safeText(data.market?.tiers?.liquidity, "流动性待识别")}</span>
            </div>
            <div className="grid grid-cols-2 gap-3 p-4 md:grid-cols-4">
              <MetricCard label="当前价格" metric={metrics.price} />
              <MetricCard label="24h 涨跌" metric={metrics.price_24h_pct} />
              <MetricCard label="24h 成交额" metric={metrics.quote_volume} />
              <MetricCard label="合约 OI" metric={metrics.oi_value} />
              <MetricCard label="15m 价格" metric={metrics.price_15m_pct} />
              <MetricCard label="1h 价格" metric={metrics.price_1h_pct} />
              <MetricCard label="15m OI" metric={metrics.oi_15m_pct} />
              <MetricCard label="资金费率" metric={metrics.funding_pct} />
            </div>
            {data.market_error ? <p className="border-t border-border-subtle px-5 py-3 text-xs text-amber-700">{data.market_error}</p> : null}
          </section>

          <section className="panel p-5">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between"><div><h2 className="section-title">信号概况</h2><p className="mt-1 text-sm text-text-muted">最近 30 条同币信号的模块分布与最新时间。</p></div><div className="table-number text-2xl font-semibold text-text-primary">{data.summary?.signal_count || 0}</div></div>
            <div className="mt-4 flex flex-wrap gap-2">{moduleCounts.length ? moduleCounts.map(([module, count]) => <span className="chip" key={module}>{module} · {count}</span>) : <span className="text-sm text-text-muted">暂无历史信号</span>}</div>
          </section>

          <section>
            <div className="mb-4"><h2 className="text-lg font-semibold text-text-primary">信号时间线</h2><p className="mt-1 text-sm text-text-muted">按时间倒序查看生命周期、排名、共振和公开摘要。</p></div>
            {timeline.length ? <div className="grid gap-4 xl:grid-cols-2">{timeline.map((item) => <SignalCard context="default" item={item} key={item.public_ref || item.id} />)}</div> : <EmptyState title="该币暂无信号" text="行情快照仍可查看；雷达产生可信信号后会自动进入时间线。" />}
          </section>
        </>
      ) : null}
    </div>
  );
}
