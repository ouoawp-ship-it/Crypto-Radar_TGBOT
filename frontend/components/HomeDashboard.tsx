"use client";

import { useState } from "react";
import { BacktestMatrix } from "./BacktestMatrix";
import { DecisionCard } from "./DecisionCard";
import { DistributionChart } from "./DistributionChart";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { MetricCard } from "./MetricCard";
import { PageTitle } from "./PageTitle";
import { SignalCard } from "./SignalCard";
import { getBacktestDecision, getBacktestMatrix, getCoinSearch, getDecisionStats, getDecisions, getOutcomeStats, getSignalStats, getLatestSignals, type HomeDashboardData } from "@/lib/api";
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
    setLoading(true);
    setError("");
    try {
      const [signalStats, signals, coins, decisionStats, decisions, outcomeStats, backtest, matrix] = await Promise.all([
        getSignalStats(),
        getLatestSignals({ limit: 8, window_sec: 86400 }),
        getCoinSearch({ limit: 10, window_sec: 604800 }),
        getDecisionStats(86400),
        getDecisions({ limit: 6, window_sec: 86400 }),
        getOutcomeStats("1h"),
        getBacktestDecision({ horizon: "1h", window_sec: 2592000 }),
        getBacktestMatrix({ window_sec: 2592000 })
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

  return (
    <div className="space-y-5">
      <PageTitle
        title="Paoxx 信号雷达"
        subtitle="加密市场信号、决策、结果追踪与回测仪表盘。公开只读展示，所有敏感字段已脱敏。"
        tags={["公开前台", "只读数据", "不执行自动交易"]}
      />

      {loading ? <div className="panel p-4 text-sm text-slate-400">正在加载数据...</div> : null}
      {data.errors?.length ? (
        <div className="panel border-warn/30 bg-warn/10 p-4 text-sm text-amber-100">
          部分数据暂时不可用：{data.errors.slice(0, 2).join("；")}
        </div>
      ) : null}

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="今日信号数" value={compact(signalTotal)} hint="最近 24 小时" />
        <MetricCard label="风险警报数" value={compact(riskCount)} tone="bad" />
        <MetricCard label="可试仓数" value={compact(probeCount)} tone="good" />
        <MetricCard label="等待回踩数" value={compact(pullbackCount)} tone="warn" />
        <MetricCard label="已追踪样本数" value={compact(trackedSamples)} />
        <MetricCard label="1h 平均最终涨跌" value={pct(data.outcomeStats?.avg_final_return_pct)} tone="neutral" />
        <MetricCard label="1h 正收益比例" value={ratioPct(data.outcomeStats?.positive_ratio)} tone="info" />
        <MetricCard label="数据覆盖率" value={ratioPct(summary.coverage_ratio)} />
      </section>

      <section className="grid gap-5 xl:grid-cols-[1.3fr_0.7fr]">
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-3">
            <h2 className="text-lg font-black text-white">最新信号卡片</h2>
            <button className="btn" onClick={load} disabled={loading}>
              {loading ? "刷新中" : "刷新"}
            </button>
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            {(data.signals || []).map((item) => (
              <SignalCard key={item.id || `${item.symbol}-${item.time}`} item={item} />
            ))}
          </div>
          {!loading && !(data.signals || []).length ? <EmptyState title="暂无信号" text="公开 API 当前没有返回最新信号，稍后会自动随扫描数据更新。" /> : null}
        </div>
        <div className="space-y-5">
          <div>
            <h2 className="mb-3 text-lg font-black text-white">决策分布</h2>
            <DistributionChart data={decisionDistribution} />
          </div>
          <div className="panel p-4">
            <h2 className="mb-3 text-lg font-black text-white">活跃币种</h2>
            <div className="flex flex-wrap gap-2">
              {(data.coins || []).map((coin) => (
                <a className="chip" href={`/coin/${encodeURIComponent(coin.symbol || "")}`} key={coin.symbol}>
                  {safeText(coin.label || coin.symbol)} · {compact(coin.count)}
                </a>
              ))}
            </div>
            {!(data.coins || []).length ? <p className="text-sm text-slate-500">暂无活跃币种数据。</p> : null}
          </div>
        </div>
      </section>

      <section className="grid gap-5 xl:grid-cols-2">
        <div>
          <h2 className="mb-3 text-lg font-black text-white">全市场决策榜</h2>
          <div className="grid gap-3">
            {(data.decisions || []).slice(0, 4).map((item) => (
              <DecisionCard key={item.symbol} item={item} />
            ))}
          </div>
          {!(data.decisions || []).length ? <EmptyState title="暂无决策数据" text="等待决策模型产生更多公开结果。" /> : null}
        </div>
        <div>
          <h2 className="mb-3 text-lg font-black text-white">决策回测摘要</h2>
          <BacktestMatrix data={data.matrix} />
        </div>
      </section>
    </div>
  );
}
