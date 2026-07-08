"use client";

import { useEffect, useState } from "react";
import { BacktestMatrix } from "./BacktestMatrix";
import { DecisionCard } from "./DecisionCard";
import { DistributionChart } from "./DistributionChart";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { MetricCard } from "./MetricCard";
import { PageTitle } from "./PageTitle";
import { SignalCard } from "./SignalCard";
import { getBacktestDecision, getBacktestMatrix, getCoinSearch, getDecisionStats, getDecisions, getOutcomeStats, getSignalStats, getSignals } from "@/lib/api";
import { compact, pct, ratioPct } from "@/lib/format";
import type { BacktestMatrixPayload, BacktestPayload, DecisionItem, SignalItem } from "@/lib/types";

type HomeState = {
  signalStats?: Record<string, unknown>;
  signals?: SignalItem[];
  coins?: Array<{ symbol?: string; label?: string; count?: number; subtitle?: string }>;
  decisionStats?: Record<string, unknown>;
  decisions?: DecisionItem[];
  outcomeStats?: Record<string, unknown>;
  backtest?: BacktestPayload;
  matrix?: BacktestMatrixPayload;
};

export function HomeDashboard() {
  const [data, setData] = useState<HomeState>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  async function load() {
    setLoading(true);
    setError("");
    try {
      const [signalStats, signals, coins, decisionStats, decisions, outcomeStats, backtest, matrix] = await Promise.all([
        getSignalStats(),
        getSignals({ limit: 8, window_sec: 86400 }),
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
        matrix
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  if (error) return <ErrorState message={error} onRetry={load} />;

  const decisionDistribution = Object.values((data.decisionStats?.distribution || {}) as Record<string, { label?: string; count?: number }>).map((item) => ({
    label: item.label || "未识别",
    count: item.count || 0
  }));
  const summary = data.backtest?.summary || {};

  return (
    <div className="space-y-5">
      <PageTitle title="Paoxx 信号雷达" subtitle="加密市场信号、决策、结果追踪与回测仪表盘。公开只读展示，所有敏感字段已脱敏。" tags={["公开前台", "只读数据", "不执行自动交易"]} />
      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="今日信号数" value={compact(data.signalStats?.total || 0)} hint={loading ? "加载中" : "最近 24 小时"} />
        <MetricCard label="风险警报数" value={compact(((data.decisionStats?.distribution as Record<string, { count?: number }> | undefined)?.risk_alert?.count) || 0)} tone="bad" />
        <MetricCard label="可试仓数" value={compact(((data.decisionStats?.distribution as Record<string, { count?: number }> | undefined)?.probe?.count) || 0)} tone="good" />
        <MetricCard label="1h 正收益比例" value={ratioPct(data.outcomeStats?.positive_ratio)} tone="info" />
        <MetricCard label="已追踪样本数" value={compact(data.outcomeStats?.success_count || 0)} />
        <MetricCard label="1h 平均最终涨跌" value={pct(data.outcomeStats?.avg_final_return_pct)} tone="neutral" />
        <MetricCard label="数据覆盖率" value={ratioPct(summary.coverage_ratio)} />
        <MetricCard label="明显回撤比例" value={ratioPct(summary.drawdown_ratio)} tone="warn" />
      </section>
      <section className="grid gap-5 xl:grid-cols-[1.3fr_0.7fr]">
        <div className="space-y-3">
          <h2 className="text-lg font-black text-white">最新信号</h2>
          <div className="grid gap-3 md:grid-cols-2">
            {(data.signals || []).map((item) => (
              <SignalCard key={item.id || `${item.symbol}-${item.time}`} item={item} />
            ))}
          </div>
          {!loading && !(data.signals || []).length ? <EmptyState title="暂无最新信号" /> : null}
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
                  {coin.label || coin.symbol} · {coin.count || 0}
                </a>
              ))}
            </div>
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
        </div>
        <div>
          <h2 className="mb-3 text-lg font-black text-white">决策回测摘要</h2>
          <BacktestMatrix data={data.matrix} />
        </div>
      </section>
    </div>
  );
}
