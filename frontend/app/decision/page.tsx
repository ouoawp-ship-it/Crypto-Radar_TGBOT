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
        title="决策中心"
        subtitle="把公开信号整理为观察、等待回踩、可试仓、禁止追高和风险警报，并展示依据、风险与观察点。"
        tags={["模型解释", "因子证据", "风险分层"]}
      />

      <section className="grid gap-5 xl:grid-cols-2">
        <div>
          <h2 className="mb-3 section-title">决策分布</h2>
          <DistributionChart data={distribution(stats, "distribution")} />
        </div>
        <div>
          <h2 className="mb-3 section-title">风险分布</h2>
          <DistributionChart data={distribution(stats, "risk_distribution")} />
        </div>
      </section>

      <section className="panel p-5">
        <div className="flex flex-col justify-between gap-4 md:flex-row md:items-end">
          <div>
            <h2 className="section-title">单币决策入口</h2>
            <p className="mt-1 text-sm text-text-muted">输入交易对后拉取当前模型结论和因子解释。</p>
          </div>
          <div className="flex flex-col gap-3 sm:flex-row">
            <input className="input min-w-64" value={symbol} placeholder="输入 BTC 或 BTCUSDT" onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
            <button className="btn" onClick={() => load(symbol, true)}>
              {loading ? "查询中" : "查询"}
            </button>
          </div>
        </div>

        {single ? <div className="mt-4"><DecisionCard item={single} /></div> : null}
        {single?.factor_explanations?.length ? (
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            {single.factor_explanations.map((factor) => (
              <div className="panel-muted p-3 text-sm text-text-secondary" key={factor.factor || factor.label}>
                <div className="font-semibold text-text-primary">{safeText(factor.label)} / {factor.score ?? "-"}</div>
                <p className="mt-1 text-text-muted">{safeText(factor.explanation, "暂无解释。")}</p>
              </div>
            ))}
          </div>
        ) : null}
      </section>

      <section className="panel p-4">
        <h2 className="mb-3 section-title">全市场决策概览</h2>
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
