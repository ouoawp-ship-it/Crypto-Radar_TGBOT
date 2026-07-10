"use client";

import { useEffect, useState } from "react";
import { DecisionCard } from "@/components/DecisionCard";
import { DistributionChart } from "@/components/DistributionChart";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { PageTitle } from "@/components/PageTitle";
import { getDecision, getDecisionStats, getDecisions, invalidatePublicApiCache } from "@/lib/api";
import { normalizeSymbol, safeText } from "@/lib/format";
import type { DecisionItem } from "@/lib/types";

function distribution(stats: Record<string, unknown>, key: "distribution" | "risk_distribution") {
  const source = (stats[key] || {}) as Record<string, { label?: string; count?: number } | number>;
  return Object.entries(source).map(([code, item]) => ({
    label: typeof item === "number" ? code : item.label || code,
    count: typeof item === "number" ? item : item.count || 0
  }));
}

export default function DecisionPage() {
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [items, setItems] = useState<DecisionItem[]>([]);
  const [stats, setStats] = useState<Record<string, unknown>>({});
  const [single, setSingle] = useState<DecisionItem | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function load(nextSymbol = symbol, refresh = false) {
    if (refresh) invalidatePublicApiCache();
    setLoading(true);
    setError("");
    try {
      const normalized = normalizeSymbol(nextSymbol || "BTCUSDT");
      const [list, statPayload, current] = await Promise.all([getDecisions({ limit: 30, window_sec: 86400 }), getDecisionStats(86400), getDecision(normalized)]);
      setItems(list.items || list.decisions || []);
      setStats(statPayload);
      setSingle(current);
      setSymbol(normalized);
    } catch (err) {
      setError(err instanceof Error ? err.message : "决策模型加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load("BTCUSDT");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) return <ErrorState message={error} onRetry={() => load(symbol, true)} />;

  return (
    <div className="space-y-5">
      <PageTitle
        title="决策模型"
        subtitle="把公开信号整理为观察、等待回踩、可试仓、禁止追高和风险警报，并展示依据、风险与观察点。"
        tags={["模型解释", "校准说明", "组成因子"]}
      />
      <section className="grid gap-5 xl:grid-cols-2">
        <div>
          <h2 className="mb-3 text-lg font-black text-white">决策分布</h2>
          <DistributionChart data={distribution(stats, "distribution")} />
        </div>
        <div>
          <h2 className="mb-3 text-lg font-black text-white">风险分布</h2>
          <DistributionChart data={distribution(stats, "risk_distribution")} />
        </div>
      </section>
      <section className="panel p-5">
        <h2 className="text-lg font-black text-white">单币决策入口</h2>
        <div className="mt-4 flex flex-col gap-3 sm:flex-row">
          <input className="input flex-1" value={symbol} placeholder="输入 BTC 或 BTCUSDT" onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
          <button className="btn" onClick={() => load(symbol, true)}>
            {loading ? "查询中" : "查询"}
          </button>
        </div>
        {single ? <div className="mt-4"><DecisionCard item={single} /></div> : null}
        {single?.factor_explanations?.length ? (
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            {single.factor_explanations.map((factor) => (
              <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-3 text-sm text-slate-300" key={factor.factor || factor.label}>
                <div className="font-bold text-white">{safeText(factor.label)} · {factor.score ?? "-"}</div>
                <p className="mt-1 text-slate-500">{safeText(factor.explanation, "暂无解释。")}</p>
              </div>
            ))}
          </div>
        ) : null}
      </section>
      <section>
        <h2 className="mb-3 text-lg font-black text-white">全市场决策榜</h2>
        <div className="grid gap-4 md:grid-cols-2">
          {items.map((item) => (
            <DecisionCard key={item.symbol} item={item} />
          ))}
        </div>
        {!loading && !items.length ? <EmptyState title="暂无决策数据" text="等待公开决策接口返回更多币种。" /> : null}
      </section>
    </div>
  );
}
