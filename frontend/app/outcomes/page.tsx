"use client";

import { useEffect, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { OutcomeCard } from "@/components/OutcomeCard";
import { PageTitle } from "@/components/PageTitle";
import { getOutcomes, getOutcomeStats, invalidatePublicApiCache } from "@/lib/api";
import { compact, pct, ratioPct } from "@/lib/format";
import type { OutcomeItem } from "@/lib/types";

export default function OutcomesPage() {
  const [horizon, setHorizon] = useState("1h");
  const [status, setStatus] = useState("");
  const [items, setItems] = useState<OutcomeItem[]>([]);
  const [stats, setStats] = useState<Record<string, unknown>>({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function load(nextHorizon = horizon, nextStatus = status, refresh = false) {
    if (refresh) invalidatePublicApiCache();
    setLoading(true);
    setError("");
    try {
      const [list, statPayload] = await Promise.all([getOutcomes({ horizon: nextHorizon, data_status: nextStatus, limit: 50 }), getOutcomeStats(nextHorizon)]);
      setItems(list.items || []);
      setStats(statPayload);
    } catch (err) {
      setError(err instanceof Error ? err.message : "结果追踪加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load("1h", "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) return <ErrorState message={error} onRetry={() => load(horizon, status, true)} />;

  return (
    <div className="space-y-5">
      <PageTitle
        title="结果追踪"
        subtitle="追踪信号推送后 1h / 4h / 24h / 72h 的最终涨跌、最高涨幅和最大回撤。"
        tags={["最终涨跌", "最高涨幅", "最大回撤"]}
      />
      <div className="panel flex flex-wrap gap-2 p-4">
        {["1h", "4h", "24h", "72h"].map((item) => (
          <button
            className={`btn ${horizon === item ? "bg-cyanline/25" : ""}`}
            key={item}
            onClick={() => {
              setHorizon(item);
              void load(item, status, true);
            }}
          >
            {item}
          </button>
        ))}
        <select
          className="input max-w-xs"
          value={status}
          onChange={(event) => {
            setStatus(event.target.value);
            void load(horizon, event.target.value, true);
          }}
        >
          <option value="">全部数据状态</option>
          <option value="success">已计算</option>
          <option value="pending">待计算</option>
          <option value="unavailable">数据不足</option>
          <option value="error">错误</option>
        </select>
      </div>
      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard label="已计算样本" value={compact(stats.success_count)} />
        <MetricCard label="待计算样本" value={compact(stats.pending_count)} />
        <MetricCard label="平均最终涨跌" value={pct(stats.avg_final_return_pct)} />
        <MetricCard label="正收益比例" value={ratioPct(stats.positive_ratio)} tone="good" />
      </section>
      <div className="panel p-4 text-sm leading-6 text-slate-400">
        数据不足通常表示当前价格源无法提供该交易对 K 线；它不会被当作系统错误，也不会当作亏损样本。
      </div>
      <section className="grid gap-4 md:grid-cols-2">
        {items.map((item, index) => (
          <OutcomeCard key={`${item.symbol}-${item.horizon}-${item.signal_time}-${index}`} item={item} />
        ))}
      </section>
      {!loading && !items.length ? <EmptyState title="暂无结果追踪样本" text="可以切换窗口或数据状态查看。" /> : null}
    </div>
  );
}
