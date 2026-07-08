"use client";

import { useEffect, useState } from "react";
import { DecisionCard } from "@/components/DecisionCard";
import { DistributionChart } from "@/components/DistributionChart";
import { ErrorState } from "@/components/ErrorState";
import { PageTitle } from "@/components/PageTitle";
import { getDecision, getDecisionStats, getDecisions } from "@/lib/api";
import type { DecisionItem } from "@/lib/types";

export default function DecisionPage() {
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [items, setItems] = useState<DecisionItem[]>([]);
  const [stats, setStats] = useState<Record<string, unknown>>({});
  const [single, setSingle] = useState<DecisionItem | null>(null);
  const [error, setError] = useState("");

  async function load() {
    setError("");
    try {
      const [list, statPayload, current] = await Promise.all([getDecisions({ limit: 30, window_sec: 86400 }), getDecisionStats(86400), getDecision(symbol)]);
      setItems(list.items || list.decisions || []);
      setStats(statPayload);
      setSingle(current);
    } catch (err) {
      setError(err instanceof Error ? err.message : "决策模型加载失败");
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) return <ErrorState message={error} onRetry={load} />;

  const distribution = Object.values((stats.distribution || {}) as Record<string, { label?: string; count?: number }>).map((item) => ({
    label: item.label || "未识别",
    count: item.count || 0
  }));

  return (
    <div className="space-y-5">
      <PageTitle title="决策模型" subtitle="把公开信号整理为观察、等待回踩、可试仓、禁止追高和风险警报，并展示依据、风险与观察点。" tags={["模型解释", "校准说明", "组成因子"]} />
      <section className="grid gap-5 xl:grid-cols-2">
        <div>
          <h2 className="mb-3 text-lg font-black text-white">决策分布</h2>
          <DistributionChart data={distribution} />
        </div>
        <div className="panel p-5">
          <h2 className="text-lg font-black text-white">单币决策入口</h2>
          <div className="mt-4 flex gap-3">
            <input className="input flex-1" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
            <button className="btn" onClick={load}>
              查询
            </button>
          </div>
          {single ? <div className="mt-4"><DecisionCard item={single} /></div> : null}
        </div>
      </section>
      <section>
        <h2 className="mb-3 text-lg font-black text-white">全市场决策榜</h2>
        <div className="grid gap-4 md:grid-cols-2">
          {items.map((item) => (
            <DecisionCard key={item.symbol} item={item} />
          ))}
        </div>
      </section>
    </div>
  );
}
