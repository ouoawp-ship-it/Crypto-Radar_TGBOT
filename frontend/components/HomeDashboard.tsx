"use client";

import Link from "next/link";
import { useState } from "react";
import { BacktestMatrix } from "./BacktestMatrix";
import { DecisionCard } from "./DecisionCard";
import { DistributionChart } from "./DistributionChart";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { MetricCard } from "./MetricCard";
import { PageTitle } from "./PageTitle";
import { SignalCard } from "./SignalCard";
import {
  getBacktestDecision,
  getBacktestMatrix,
  getCoinSearch,
  getDecisionStats,
  getDecisions,
  getLifecycleIntelligenceSummary,
  getLifecycleSummary,
  getOutcomeStats,
  getSignalStats,
  getLatestSignals,
  invalidatePublicApiCache,
  type HomeDashboardData
} from "@/lib/api";
import { compact, pct, ratioPct, safeText } from "@/lib/format";

function readNumber(record: Record<string, unknown> | undefined, ...keys: string[]) {
  for (const key of keys) {
    const value = record?.[key];
    if (typeof value === "number") return value;
    if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  }
  return undefined;
}

function distributionItems(stats?: Record<string, unknown>) {
  const distribution = (stats?.distribution || stats?.by_decision || {}) as Record<string, { label?: string; count?: number }>;
  return Object.entries(distribution).map(([key, item]) => ({
    label: item?.label || key,
    count: item?.count || 0
  }));
}

function distributionCount(stats: Record<string, unknown> | undefined, code: string) {
  const distribution = (stats?.distribution || {}) as Record<string, { count?: number }>;
  return distribution?.[code]?.count;
}

export function HomeDashboard({ initialData = {} }: { initialData?: HomeDashboardData }) {
  const [data, setData] = useState<HomeDashboardData>(initialData);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    invalidatePublicApiCache();
    setLoading(true);
    setError("");
    try {
      const [signalStats, signals, coins, decisionStats, decisions, outcomeStats, backtest, matrix, lifecycle, lifecycleIntelligence] = await Promise.all([
        getSignalStats(),
        getLatestSignals({ limit: 8, window_sec: 86400 }),
        getCoinSearch({ limit: 10, window_sec: 604800 }),
        getDecisionStats(86400),
        getDecisions({ limit: 6, window_sec: 86400 }),
        getOutcomeStats("1h"),
        getBacktestDecision({ horizon: "1h", window_sec: 2592000 }),
        getBacktestMatrix({ window_sec: 2592000 }),
        getLifecycleSummary(),
        getLifecycleIntelligenceSummary()
      ]);
      setData({
        signalStats,
        signals: signals.items || [],
        coins: coins.items || [],
        decisionStats,
        decisions: decisions.items || decisions.decisions || [],
        outcomeStats,
        backtest,
        matrix,
        lifecycle,
        lifecycleIntelligence,
        errors: []
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "数据暂时不可用，请稍后重试。");
    } finally {
      setLoading(false);
    }
  }

  if (error && !data.signalStats && !data.signals?.length) return <ErrorState message={error} onRetry={load} />;

  const decisionDistribution = distributionItems(data.decisionStats);
  const summary = data.backtest?.summary || {};
  const signalTotal = readNumber(data.signalStats, "total", "count", "signals_count");
  const trackedSamples = readNumber(data.outcomeStats, "success_count", "total");
  const probeCount = distributionCount(data.decisionStats, "probe");
  const riskCount = distributionCount(data.decisionStats, "risk_alert");
  const pullbackCount = distributionCount(data.decisionStats, "wait_pullback");
  const lifecycleSummary = data.lifecycle?.summary || {};
  const hasAnyData = Boolean(
    data.signalStats ||
      (data.signals || []).length ||
      (data.coins || []).length ||
      data.decisionStats ||
      (data.decisions || []).length ||
      data.outcomeStats ||
      data.backtest ||
      data.matrix ||
      data.lifecycle ||
      data.lifecycleIntelligence
  );

  return (
    <div className="space-y-5">
      <PageTitle
        title="总览"
        subtitle="把信号、决策、生命周期和回测表现集中到一个工作台，优先展示当前市场是否值得行动。"
        tags={["结论优先", "只读公开数据", "非投资建议"]}
      />

      {loading ? <div className="panel p-4 text-sm text-text-secondary">正在刷新公开数据...</div> : null}
      {data.errors?.length && !hasAnyData ? (
        <div className="panel border-warn/25 bg-warn/5 p-4 text-sm text-amber-700">
          公开数据暂时不可用：{data.errors.slice(0, 2).join("；")}
        </div>
      ) : null}

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="今日信号数" value={compact(signalTotal)} hint="最近 24 小时" tone="info" />
        <MetricCard label="风险警报" value={compact(riskCount)} tone="bad" />
        <MetricCard label="可试仓" value={compact(probeCount)} tone="good" />
        <MetricCard label="等待回踩" value={compact(pullbackCount)} tone="warn" />
        <MetricCard label="追踪样本" value={compact(trackedSamples)} />
        <MetricCard label="1h 平均收益" value={pct(data.outcomeStats?.avg_final_return_pct)} />
        <MetricCard label="1h 正收益率" value={ratioPct(data.outcomeStats?.positive_ratio)} tone="info" />
        <MetricCard label="覆盖率" value={ratioPct(summary.coverage_ratio)} />
      </section>

      <section className="grid gap-5 xl:grid-cols-[1.35fr_0.65fr]">
        <div className="panel p-4">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <h2 className="section-title">最新信号卡片</h2>
              <p className="mt-1 text-sm text-text-muted">按时间倒序展示，可进入单币详情继续追踪证据链。</p>
            </div>
            <button className="btn-secondary" onClick={load} disabled={loading}>
              {loading ? "刷新中" : "刷新"}
            </button>
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            {(data.signals || []).map((item) => (
              <SignalCard key={item.id || `${item.symbol}-${item.time}`} item={item} />
            ))}
          </div>
          {!loading && !(data.signals || []).length ? <EmptyState title="暂无信号" text="公开 API 当前没有返回最新信号。" /> : null}
        </div>

        <div className="space-y-5">
          <div>
            <h2 className="mb-3 section-title">决策分布</h2>
            <DistributionChart data={decisionDistribution} />
          </div>
          <div className="panel p-4">
            <h2 className="section-title">活跃币种</h2>
            <div className="mt-3 flex flex-wrap gap-2">
              {(data.coins || []).map((coin) => (
                <Link className="chip" href={`/coin/${encodeURIComponent(coin.symbol || "")}`} key={coin.symbol}>
                  {safeText(coin.label || coin.symbol)} / {compact(coin.count)}
                </Link>
              ))}
            </div>
            {!(data.coins || []).length ? <p className="mt-3 text-sm text-text-muted">暂无活跃币种数据。</p> : null}
          </div>
        </div>
      </section>

      <section className="grid gap-5 xl:grid-cols-2">
        <div className="panel p-4">
          <h2 className="mb-3 section-title">全市场决策概览</h2>
          <div className="grid gap-3">
            {(data.decisions || []).slice(0, 4).map((item) => (
              <DecisionCard key={item.symbol} item={item} />
            ))}
          </div>
          {!(data.decisions || []).length ? <EmptyState title="暂无决策数据" text="等待决策模型产生更多公开结果。" /> : null}
        </div>
        <div>
          <h2 className="mb-3 section-title">决策回测摘要</h2>
          <BacktestMatrix data={data.matrix} />
        </div>
      </section>

      <section className="panel p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <h2 className="section-title">结果追踪</h2>
            <p className="mt-1 text-sm text-text-muted">用成熟样本回看决策是否兑现，避免只看信号强度。</p>
          </div>
          <Link className="btn-secondary" href="/outcomes">
            查看结果追踪
          </Link>
        </div>
        <div className="grid gap-4 md:grid-cols-3">
          <MetricCard label="已追踪样本" value={compact(trackedSamples)} />
          <MetricCard label="1h 平均最终涨跌" value={pct(data.outcomeStats?.avg_final_return_pct)} />
          <MetricCard label="1h 正收益比例" value={ratioPct(data.outcomeStats?.positive_ratio)} tone="info" />
        </div>
      </section>

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="活跃生命周期" value={compact(lifecycleSummary.active_count)} tone="info" />
        <MetricCard
          label="周期升级"
          value={compact(Number(lifecycleSummary.upgraded_1h_count || 0) + Number(lifecycleSummary.upgraded_4h_count || 0) + Number(lifecycleSummary.trend_confirmed_count || 0))}
          tone="good"
        />
        <MetricCard label="风险升高" value={compact(lifecycleSummary.risk_warning_count)} tone="bad" />
        <MetricCard label="短线冷却" value={compact(lifecycleSummary.cooling_count)} tone="warn" />
      </section>

      <section className="panel p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <h2 className="section-title">生命周期智能榜</h2>
            <p className="mt-1 text-sm text-text-muted">预计算 TOP 5，优先查看质量标签、最高周期和风险评分。</p>
          </div>
          <Link className="btn-secondary" href="/lifecycle">
            查看智能排行
          </Link>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
          {(data.lifecycleIntelligence?.items || []).slice(0, 5).map((item) => (
            <Link className="panel-muted block p-3 transition hover:border-primary-100 hover:bg-white" href={`/coin/${encodeURIComponent(item.symbol || "")}`} key={item.symbol}>
              <h3 className="table-number text-base font-semibold text-text-primary">{safeText(item.symbol)}</h3>
              <p className="mt-1 text-sm text-primary-700">{safeText(item.quality_label, "历史样本仍在积累")}</p>
              <p className="mt-3 text-sm text-text-secondary">最高周期 {safeText(item.highest_level, "-")}</p>
              <p className="text-sm text-text-secondary">智能 {compact(item.intelligence_score)} / 风险 {compact(item.risk_score)}</p>
            </Link>
          ))}
        </div>
        {!(data.lifecycleIntelligence?.items || []).length ? <p className="text-sm text-text-muted">历史样本仍在积累。</p> : null}
      </section>
    </div>
  );
}
