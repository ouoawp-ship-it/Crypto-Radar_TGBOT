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
import { getBacktestDetail, getCoinDetail, getDecision, getLifecycleDetail, getSymbolOutcomes, getSymbolTimeline } from "@/lib/api";
import { compact, normalizeSymbol, pct, safeText } from "@/lib/format";
import type { DecisionItem, LifecycleDetailPayload, OutcomeItem, SignalItem } from "@/lib/types";

export default function CoinPage() {
  const params = useParams<{ symbol: string }>();
  const initialSymbol = normalizeSymbol(decodeURIComponent(params.symbol || ""));
  const [symbol, setSymbol] = useState(initialSymbol);
  const [detail, setDetail] = useState<Record<string, unknown>>({});
  const [decision, setDecision] = useState<DecisionItem | null>(null);
  const [timeline, setTimeline] = useState<SignalItem[]>([]);
  const [outcomes, setOutcomes] = useState<OutcomeItem[]>([]);
  const [samples, setSamples] = useState<OutcomeItem[]>([]);
  const [lifecycle, setLifecycle] = useState<LifecycleDetailPayload>({});
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
      const [coin, currentDecision, timelinePayload, outcomePayload, backtestPayload, lifecyclePayload] = await Promise.all([
        getCoinDetail(normalized),
        getDecision(normalized),
        getSymbolTimeline(normalized),
        getSymbolOutcomes(normalized, { limit: 20 }),
        getBacktestDetail({ symbol: normalized, limit: 10, window_sec: 2592000 }),
        getLifecycleDetail(normalized)
      ]);
      setSymbol(normalized);
      setDetail(coin);
      setDecision(currentDecision);
      setTimeline(timelinePayload.items || (timelinePayload.groups || []).flatMap((group) => group.items || []));
      setOutcomes(outcomePayload.items || []);
      setSamples(backtestPayload.items || []);
      setLifecycle(lifecyclePayload);
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
      {lifecycle.lifecycle ? (
        <section className="panel p-4">
          <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-lg font-black text-white">生命周期状态</h2>
              <p className="text-sm text-slate-400">首次信号 {safeText(lifecycle.lifecycle.first_signal_level, "-")} · 最高周期 {safeText(lifecycle.lifecycle.highest_level, "-")} · {safeText(lifecycle.lifecycle.state_label, "启动观察")}</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <span className="chip">强度 {compact(lifecycle.lifecycle.lifecycle_score)}</span>
              <span className="chip">风险 {compact(lifecycle.lifecycle.risk_score)}</span>
            </div>
          </div>
          <div className="grid gap-3 text-sm text-slate-300 md:grid-cols-3">
            <span>首次信号：{safeText(lifecycle.lifecycle.first_signal_at, "-")}</span>
            <span>最新信号：{safeText(lifecycle.lifecycle.latest_signal_at, "-")}</span>
            <span>Binance 价格：{pct(lifecycle.lifecycle.price_change_from_first_pct)}</span>
            <span>Binance OI：{pct(lifecycle.lifecycle.oi_change_from_first_pct)}</span>
            <span>合约 CVD：{safeText(lifecycle.lifecycle.futures_cvd_status, "数据不足")}</span>
            <span>现货 CVD：{safeText(lifecycle.lifecycle.spot_cvd_status, "数据不足")}</span>
            <span>资金费率：{safeText(lifecycle.lifecycle.funding_status, "数据不足")}</span>
            <span>旁路观察：其他交易所仅看当前价格和资金费率</span>
            <span>{safeText(lifecycle.lifecycle.not_advice, "仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。")}</span>
          </div>
          <h3 className="mt-5 text-base font-black text-white">生命周期事件时间线</h3>
          <div className="mt-3 grid gap-2">
            {(lifecycle.events || []).slice(0, 8).map((event, index) => (
              <div className="rounded-lg border border-white/10 bg-white/[0.03] p-3 text-sm text-slate-300" key={`${event.event_time}-${index}`}>
                <b className="text-white">{safeText(event.event_label || event.event_type)}</b>
                <span className="ml-2 text-slate-500">{safeText(event.event_time)}</span>
                <p className="mt-1">周期 {safeText(event.event_level, "-")}，价格 {pct(event.price_change_from_first_pct)}，OI {pct(event.oi_change_pct)}</p>
              </div>
            ))}
          </div>
        </section>
      ) : (
        <EmptyState title="暂无生命周期状态" text="该币种首次出现有效信号后会自动建档并持续跟随。" />
      )}
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
