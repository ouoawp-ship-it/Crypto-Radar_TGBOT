"use client";

import { useEffect, useState } from "react";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { OutcomeCard } from "@/components/OutcomeCard";
import { PageTitle } from "@/components/PageTitle";
import { getOutcomes, getOutcomeStats } from "@/lib/api";
import { pct, ratioPct } from "@/lib/format";
import type { OutcomeItem } from "@/lib/types";

export default function OutcomesPage() {
  const [horizon, setHorizon] = useState("1h");
  const [items, setItems] = useState<OutcomeItem[]>([]);
  const [stats, setStats] = useState<Record<string, unknown>>({});
  const [error, setError] = useState("");

  async function load(nextHorizon = horizon) {
    setError("");
    try {
      const [list, statPayload] = await Promise.all([getOutcomes({ horizon: nextHorizon, limit: 50 }), getOutcomeStats(nextHorizon)]);
      setItems(list.items || []);
      setStats(statPayload);
    } catch (err) {
      setError(err instanceof Error ? err.message : "结果追踪加载失败");
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) return <ErrorState message={error} onRetry={() => load()} />;

  return (
    <div className="space-y-5">
      <PageTitle title="结果追踪" subtitle="追踪信号推送后 1h / 4h / 24h / 72h 的最终涨跌、最高涨幅和最大回撤。" tags={["最终涨跌", "最高涨幅", "最大回撤"]} />
      <div className="panel flex flex-wrap gap-2 p-4">
        {["1h", "4h", "24h", "72h"].map((item) => (
          <button
            className={`btn ${horizon === item ? "bg-cyanline/25" : ""}`}
            key={item}
            onClick={() => {
              setHorizon(item);
              void load(item);
            }}
          >
            {item}
          </button>
        ))}
      </div>
      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard label="已计算样本" value={stats.success_count || 0} />
        <MetricCard label="平均最终涨跌" value={pct(stats.avg_final_return_pct)} />
        <MetricCard label="正收益比例" value={ratioPct(stats.positive_ratio)} tone="good" />
        <MetricCard label="明显回撤比例" value={ratioPct(stats.drawdown_ratio)} tone="warn" />
      </section>
      <div className="panel p-4 text-sm leading-6 text-slate-400">
        数据不足通常表示当前价格源无法提供该交易对 K 线；它不会被当作系统错误，也不会当作亏损样本。
      </div>
      <section className="grid gap-4 md:grid-cols-2">
        {items.map((item, index) => (
          <OutcomeCard key={`${item.symbol}-${item.horizon}-${item.signal_time}-${index}`} item={item} />
        ))}
      </section>
    </div>
  );
}
