"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { DecisionCard } from "@/components/DecisionCard";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { OutcomeCard } from "@/components/OutcomeCard";
import { PageTitle } from "@/components/PageTitle";
import { SignalCard } from "@/components/SignalCard";
import { getBacktestDetail, getCoinDetail, getDecision, getSymbolOutcomes, getSymbolTimeline } from "@/lib/api";
import type { DecisionItem, OutcomeItem, SignalItem } from "@/lib/types";

export default function CoinPage() {
  const params = useParams<{ symbol: string }>();
  const symbol = decodeURIComponent(params.symbol || "").toUpperCase();
  const [detail, setDetail] = useState<Record<string, unknown>>({});
  const [decision, setDecision] = useState<DecisionItem | null>(null);
  const [timeline, setTimeline] = useState<SignalItem[]>([]);
  const [outcomes, setOutcomes] = useState<OutcomeItem[]>([]);
  const [samples, setSamples] = useState<OutcomeItem[]>([]);
  const [error, setError] = useState("");

  async function load() {
    setError("");
    try {
      const [coin, currentDecision, timelinePayload, outcomePayload, backtestPayload] = await Promise.all([
        getCoinDetail(symbol),
        getDecision(symbol),
        getSymbolTimeline(symbol),
        getSymbolOutcomes(symbol, { limit: 20 }),
        getBacktestDetail({ symbol, limit: 10, window_sec: 2592000 })
      ]);
      setDetail(coin);
      setDecision(currentDecision);
      setTimeline(timelinePayload.items || (timelinePayload.groups || []).flatMap((group) => group.items || []));
      setOutcomes(outcomePayload.items || []);
      setSamples(backtestPayload.items || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "币种详情加载失败");
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol]);

  if (error) return <ErrorState message={error} onRetry={load} />;

  const summary = (detail.summary || {}) as Record<string, unknown>;

  return (
    <div className="space-y-5">
      <PageTitle title={`${symbol} 币种详情`} subtitle="单币信号、当前决策、结果追踪与回测样本集中查看。" tags={["单币视角", "信号历史", "结果复盘"]} />
      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard label="最近信号数" value={summary.total || 0} />
        <MetricCard label="已发送" value={summary.sent || 0} tone="good" />
        <MetricCard label="活跃模块" value={summary.active_modules || 0} />
        <MetricCard label="健康状态" value={summary.health_label || summary.health || "观察"} />
      </section>
      {decision ? <DecisionCard item={decision} /> : null}
      <section className="grid gap-5 xl:grid-cols-2">
        <div>
          <h2 className="mb-3 text-lg font-black text-white">信号历史</h2>
          <div className="grid gap-3">
            {timeline.slice(0, 12).map((item) => (
              <SignalCard key={item.id || `${item.time}-${item.module}`} item={item} />
            ))}
          </div>
        </div>
        <div>
          <h2 className="mb-3 text-lg font-black text-white">历史结果追踪</h2>
          <div className="grid gap-3">
            {outcomes.slice(0, 12).map((item, index) => (
              <OutcomeCard key={`${item.signal_time}-${index}`} item={item} />
            ))}
          </div>
        </div>
      </section>
      <section>
        <h2 className="mb-3 text-lg font-black text-white">回测样本</h2>
        <div className="grid gap-3 md:grid-cols-2">
          {samples.map((item, index) => (
            <OutcomeCard key={`${item.signal_time}-sample-${index}`} item={item} />
          ))}
        </div>
      </section>
      <a className="btn inline-flex" href={`https://www.tradingview.com/chart/?symbol=BINANCE:${encodeURIComponent(symbol)}`} target="_blank" rel="noreferrer">
        打开外部图表
      </a>
    </div>
  );
}
