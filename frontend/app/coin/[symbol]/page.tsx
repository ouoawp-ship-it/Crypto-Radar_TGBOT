"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { DecisionCard } from "@/components/DecisionCard";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { OutcomeCard } from "@/components/OutcomeCard";
import { PageTitle } from "@/components/PageTitle";
import { SignalCard } from "@/components/SignalCard";
import { getBacktestDetail, getCoinDetail, getDecision, getSymbolOutcomes, getSymbolTimeline } from "@/lib/api";
import { compact, normalizeSymbol, safeText } from "@/lib/format";
import type { DecisionItem, OutcomeItem, SignalItem } from "@/lib/types";

export default function CoinPage() {
  const params = useParams<{ symbol: string }>();
  const initialSymbol = normalizeSymbol(decodeURIComponent(params.symbol || ""));
  const [symbol, setSymbol] = useState(initialSymbol);
  const [detail, setDetail] = useState<Record<string, unknown>>({});
  const [decision, setDecision] = useState<DecisionItem | null>(null);
  const [timeline, setTimeline] = useState<SignalItem[]>([]);
  const [outcomes, setOutcomes] = useState<OutcomeItem[]>([]);
  const [samples, setSamples] = useState<OutcomeItem[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function load(nextSymbol = symbol) {
    const normalized = normalizeSymbol(nextSymbol);
    if (!normalized) {
      setError("请提供币种，例如 BTCUSDT。");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const [coin, currentDecision, timelinePayload, outcomePayload, backtestPayload] = await Promise.all([
        getCoinDetail(normalized),
        getDecision(normalized),
        getSymbolTimeline(normalized),
        getSymbolOutcomes(normalized, { limit: 20 }),
        getBacktestDetail({ symbol: normalized, limit: 10, window_sec: 2592000 })
      ]);
      setSymbol(normalized);
      setDetail(coin);
      setDecision(currentDecision);
      setTimeline(timelinePayload.items || (timelinePayload.groups || []).flatMap((group) => group.items || []));
      setOutcomes(outcomePayload.items || []);
      setSamples(backtestPayload.items || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "币种详情加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load(initialSymbol);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSymbol]);

  if (error) return <ErrorState message={error} onRetry={() => load()} />;

  const summary = (detail.summary || {}) as Record<string, unknown>;

  return (
    <div className="space-y-5">
      <PageTitle
        title={`${symbol} 币种详情`}
        subtitle="集中查看单币信号、当前决策、结果追踪与回测样本。"
        tags={["单币视角", "信号历史", "结果复盘"]}
      />
      <section className="panel flex flex-col gap-3 p-4 sm:flex-row">
        <input className="input flex-1" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} placeholder="输入 BTC 或 BTCUSDT" />
        <button className="btn" onClick={() => load(symbol)}>
          {loading ? "加载中" : "切换币种"}
        </button>
        <a className="btn inline-flex items-center justify-center" href={`https://www.tradingview.com/chart/?symbol=BINANCE:${encodeURIComponent(symbol)}`} target="_blank" rel="noreferrer">
          外部图表
        </a>
      </section>
      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard label="最近信号数" value={compact(summary.total)} />
        <MetricCard label="已发送" value={compact(summary.sent)} tone="good" />
        <MetricCard label="活跃模块" value={compact(summary.active_modules)} />
        <MetricCard label="健康状态" value={safeText(summary.health_label || summary.health, "观察")} />
      </section>
      {decision ? <DecisionCard item={decision} /> : <EmptyState title="暂无当前决策" text="等待更多同币种信号后会生成决策。" />}
      <section className="grid gap-5 xl:grid-cols-2">
        <div>
          <h2 className="mb-3 text-lg font-black text-white">信号历史</h2>
          <div className="grid gap-3">
            {timeline.slice(0, 12).map((item) => (
              <SignalCard key={item.id || `${item.time}-${item.module}`} item={item} />
            ))}
          </div>
          {!timeline.length && !loading ? <EmptyState title="暂无信号历史" text="该币种当前没有公开时间线事件。" /> : null}
        </div>
        <div>
          <h2 className="mb-3 text-lg font-black text-white">历史结果追踪</h2>
          <div className="grid gap-3">
            {outcomes.slice(0, 12).map((item, index) => (
              <OutcomeCard key={`${item.signal_time}-${index}`} item={item} />
            ))}
          </div>
          {!outcomes.length && !loading ? <EmptyState title="暂无结果追踪" text="等待该币种信号窗口到期并完成计算。" /> : null}
        </div>
      </section>
      <section>
        <h2 className="mb-3 text-lg font-black text-white">回测样本</h2>
        <div className="grid gap-3 md:grid-cols-2">
          {samples.map((item, index) => (
            <OutcomeCard key={`${item.signal_time}-sample-${index}`} item={item} />
          ))}
        </div>
        {!samples.length && !loading ? <EmptyState title="暂无回测样本" text="该币种还没有可公开展示的回测样本。" /> : null}
      </section>
    </div>
  );
}
