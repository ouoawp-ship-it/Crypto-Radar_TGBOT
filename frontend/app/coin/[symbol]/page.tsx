"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { DecisionCard } from "@/components/DecisionCard";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { OutcomeCard } from "@/components/OutcomeCard";
import { PageTitle } from "@/components/PageTitle";
import { SignalCard } from "@/components/SignalCard";
import { getBacktestDetail, getCoinDetail, getDecision, getLifecycleDetail, getLifecycleIntelligenceDetail, getLifecycleOutcomeDetail, getLifecycleOutcomeQualitySummary, getLifecycleSimilar, getSymbolOutcomes, getSymbolTimeline, invalidatePublicApiCache } from "@/lib/api";
import { compact, normalizeSymbol, pct, ratioPct, safeText } from "@/lib/format";
import type { DecisionItem, LifecycleDetailPayload, LifecycleIntelligenceDetailPayload, LifecycleOutcomeDetailPayload, LifecycleOutcomeQualitySummaryPayload, LifecycleSimilarityPayload, OutcomeItem, SignalItem } from "@/lib/types";

function lifecycleOutcomeStatus(detail: LifecycleOutcomeDetailPayload, horizon: string): string {
  const coverage = detail.coverage as Record<string, unknown> | null | undefined;
  const status = coverage?.[`horizon_${horizon}_status`];
  if (typeof status === "string") return status;
  const value = detail.horizons?.[horizon];
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    for (const key of ["success", "unavailable", "error", "not_due", "pending", "ready", "missing"]) {
      if (Number(value[key as keyof typeof value] || 0) > 0) return key;
    }
  }
  return "missing";
}

function lifecycleOutcomeStatusLabel(status: string): string {
  return ({
    success: "已成功计算",
    not_due: "尚未到期",
    pending: "等待扫描",
    ready: "待计算",
    unavailable: "数据不可用",
    error: "计算异常",
    missing: "尚无记录"
  } as Record<string, string>)[status] || status;
}

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
  const [intelligence, setIntelligence] = useState<LifecycleIntelligenceDetailPayload>({});
  const [similar, setSimilar] = useState<LifecycleSimilarityPayload>({});
  const [outcomeDetail, setOutcomeDetail] = useState<LifecycleOutcomeDetailPayload>({});
  const [outcomeQuality, setOutcomeQuality] = useState<LifecycleOutcomeQualitySummaryPayload>({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function load(nextSymbol = symbol, refresh = false) {
    if (refresh) invalidatePublicApiCache();
    const normalized = normalizeSymbol(nextSymbol);
    if (!normalized) {
      setError("请提供币种，例如 BTCUSDT。");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const [coin, currentDecision, timelinePayload, outcomePayload, backtestPayload, lifecyclePayload, intelligencePayload, similarPayload, lifecycleOutcomePayload, lifecycleOutcomeQualityPayload] = await Promise.all([
        getCoinDetail(normalized),
        getDecision(normalized),
        getSymbolTimeline(normalized),
        getSymbolOutcomes(normalized, { limit: 20 }),
        getBacktestDetail({ symbol: normalized, limit: 10, window_sec: 2592000 }),
        getLifecycleDetail(normalized),
        getLifecycleIntelligenceDetail(normalized),
        getLifecycleSimilar(normalized, 5),
        getLifecycleOutcomeDetail(normalized).catch(() => ({} as LifecycleOutcomeDetailPayload)),
        getLifecycleOutcomeQualitySummary({ symbol: normalized }).catch(() => ({} as LifecycleOutcomeQualitySummaryPayload))
      ]);
      setSymbol(normalized);
      setDetail(coin);
      setDecision(currentDecision);
      setTimeline(timelinePayload.items || (timelinePayload.groups || []).flatMap((group) => group.items || []));
      setOutcomes(outcomePayload.items || []);
      setSamples(backtestPayload.items || []);
      setLifecycle(lifecyclePayload);
      setIntelligence(intelligencePayload);
      setSimilar(similarPayload);
      setOutcomeDetail(lifecycleOutcomePayload);
      setOutcomeQuality(lifecycleOutcomeQualityPayload);
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

  if (error) return <ErrorState message={error} onRetry={() => load(symbol, true)} />;

  const summary = (detail.summary || {}) as Record<string, unknown>;
  const lifecycleMetrics = lifecycle.lifecycle?.metrics || {};
  const exchangeContext = lifecycle.lifecycle?.exchange_context || {};
  const exchangeItems = (Array.isArray(exchangeContext.items) ? exchangeContext.items : []).filter(
    (item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item)
  );
  const outcomeCoverage = outcomeDetail.coverage;
  const primaryLink = outcomeDetail.links?.find((item) => Boolean(item.is_primary));
  const primaryOutcome = (primaryLink?.outcome || primaryLink || outcomeDetail.primary_outcome || outcomeDetail.primary) as {
    final_return_pct?: number | null;
    max_gain_pct?: number | null;
    max_drawdown_pct?: number | null;
  } | undefined;
  const coinQuality = {
    ...(outcomeDetail.candidate_quality || outcomeDetail.quality || {}),
    ...(outcomeQuality.summary || {}),
    ...outcomeQuality
  } as LifecycleOutcomeQualitySummaryPayload;
  const coinQualityRecord = {
    ...(coinQuality.status_counts || {}),
    ...(coinQuality as Record<string, unknown>)
  } as Record<string, unknown>;
  const qualityCount = (...keys: string[]): number => {
    for (const key of keys) {
      const value = Number(coinQualityRecord[key]);
      if (Number.isFinite(value)) return value;
    }
    return 0;
  };
  const qualityReasons = coinQuality.reasons || coinQuality.top_gap_reasons || {};

  return (
    <div className="space-y-5">
      <PageTitle
        title={`${symbol} 币种详情`}
        subtitle="集中查看单币信号、当前决策、结果追踪与回测样本。"
        tags={["单币视角", "信号历史", "结果复盘"]}
      />
      <section className="panel flex flex-col gap-3 p-4 sm:flex-row">
        <input className="input flex-1" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} placeholder="输入 BTC 或 BTCUSDT" />
        <button className="btn" onClick={() => load(symbol, true)}>
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
      <section className="panel p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-black text-white">生命周期智能评价</h2>
            <p className="mt-1 text-sm text-slate-400">智能评分、当前阶段、资金确认状态、风险因素与观察点。</p>
          </div>
          <Link className="btn" href={`/lifecycle/replay?symbol=${encodeURIComponent(symbol)}`}>打开回放</Link>
        </div>
        {intelligence.intelligence ? (
          <div className="mt-4 space-y-4">
            <div className="grid gap-4 md:grid-cols-4">
              <MetricCard label="智能评分" value={compact(intelligence.intelligence.intelligence_score)} tone="info" />
              <MetricCard label="生命周期质量评分" value={safeText(intelligence.intelligence.quality_label, "-")} tone="good" />
              <MetricCard label="当前阶段" value={safeText(intelligence.intelligence.stage_label, "-")} />
              <MetricCard label="风险" value={safeText(intelligence.intelligence.risk_label, "-")} tone="warn" />
            </div>
            <p className="text-sm text-slate-300">资金确认状态：{safeText(intelligence.intelligence.capital_confirmation_label, "数据不足")}</p>
            <p className="text-sm text-slate-300">{safeText(intelligence.intelligence.summary, "智能评价摘要仍在生成")}</p>
            <div className="grid gap-3 md:grid-cols-2">
              <div className="rounded-lg border border-white/10 p-3"><b className="text-white">风险因素</b><p className="mt-2 text-sm text-slate-400">{(intelligence.intelligence.risks || []).join("；") || "暂未识别显著风险因素"}</p></div>
              <div className="rounded-lg border border-white/10 p-3"><b className="text-white">观察点</b><p className="mt-2 text-sm text-slate-400">{(intelligence.intelligence.watch_points || []).join("；") || "等待后续生命周期事件"}</p></div>
            </div>
          </div>
        ) : (
          <p className="mt-4 text-sm text-slate-500">历史样本仍在积累</p>
        )}
        <div className="mt-4 rounded-lg border border-white/10 p-3">
          <b className="text-white">历史相似生命周期</b>
          <p className="mt-2 text-sm text-slate-400">相似样本 {compact(similar.similar_count)} · {safeText(similar.message || similar.disclaimer, "当前相似样本不足，暂不生成统计结论。")}</p>
          {(similar.samples || []).length ? (
            <div className="mt-3 grid gap-2">
              {(similar.samples || []).slice(0, 5).map((sample, index) => {
                const sampleSymbol = safeText(sample.symbol, "-");
                return (
                  <Link className="rounded-lg border border-white/10 p-3 text-sm text-slate-300 hover:border-cyan-400/40" href={`/coin/${encodeURIComponent(sampleSymbol)}`} key={`${safeText(sample.lifecycle_id, String(index))}-${sampleSymbol}`}>
                    <b className="text-white">{sampleSymbol}</b>
                    <span className="ml-3">相似度 {compact(sample.similarity_score)}</span>
                    <span className="ml-3">{safeText(sample.upgrade_path, "-")}</span>
                    <span className="ml-3">结果 {safeText(sample.result_label, "数据不足")}</span>
                    <span className="ml-3">收益 {pct(sample.final_return_pct)}</span>
                  </Link>
                );
              })}
            </div>
          ) : null}
        </div>
      </section>
      <section className="panel space-y-4 p-4">
        <div>
          <h2 className="text-lg font-black text-white">Outcome 关联卡</h2>
          <p className="mt-1 text-sm text-slate-400">按 lifecycle signal_id 确定性关联；尚未到期不是失败，数据不可用也不等于亏损。</p>
        </div>
        <div className="grid gap-4 md:grid-cols-4">
          {(["1h", "4h", "24h", "72h"] as const).map((horizon) => {
            const status = lifecycleOutcomeStatus(outcomeDetail, horizon);
            const tone = status === "success" ? "good" : status === "error" ? "bad" : status === "unavailable" ? "warn" : "info";
            return <MetricCard key={horizon} label={`${horizon} Outcome`} value={lifecycleOutcomeStatusLabel(status)} tone={tone} />;
          })}
        </div>
        <div className="grid gap-3 text-sm text-slate-300 md:grid-cols-3">
          <span>最终涨跌：{pct(primaryOutcome?.final_return_pct)}</span>
          <span>最高涨幅：{pct(primaryOutcome?.max_gain_pct)}</span>
          <span>最大回撤：{pct(primaryOutcome?.max_drawdown_pct)}</span>
          <span>关联来源：{safeText(primaryLink?.link_method || outcomeDetail.link_method, "尚未关联")}</span>
          <span>数据成熟度：{safeText(outcomeCoverage?.maturity_label, "等待到期")}</span>
          <span>关联覆盖率：{ratioPct(outcomeCoverage?.link_coverage_ratio)}</span>
        </div>
      </section>
      <section className="panel space-y-4 p-4">
        <div>
          <h2 className="text-lg font-black text-white">Outcome 数据质量</h2>
          <p className="mt-1 text-sm text-slate-400">该币 Lifecycle Outcome 候选的资格、关联、成熟与补算状态；不显示内部候选 ID 或 Outcome ID。</p>
        </div>
        <div className="grid gap-4 md:grid-cols-3 xl:grid-cols-6">
          <MetricCard label="Outcome 候选数" value={compact(qualityCount("candidate_count", "outcome_candidate_count"))} />
          <MetricCard label="已关联" value={compact(qualityCount("linked_candidate_count", "linked_count"))} tone="good" />
          <MetricCard label="已成熟周期" value={compact(qualityCount("success", "successful_due_candidate_count") || outcomeDetail.mature_horizons?.length)} tone="good" />
          <MetricCard label="等待到期" value={compact(qualityCount("not_due", "not_due_count") || outcomeDetail.pending_horizons?.length)} tone="info" />
          <MetricCard label="数据不可用周期" value={compact(qualityCount("terminal_unavailable", "unavailable") || outcomeDetail.unavailable_horizons?.length)} tone="warn" />
          <MetricCard label="可重试" value={compact(qualityCount("retry_wait", "retryable_count"))} tone="warn" />
        </div>
        <div className="grid gap-3 text-sm text-slate-300 md:grid-cols-2">
          <div className="rounded-lg border border-white/10 p-3">
            <b className="text-white">缺口原因</b>
            <div className="mt-2 flex flex-wrap gap-2">
              {Object.entries(qualityReasons).slice(0, 8).map(([reason, count]) => <span className="chip" key={reason}>{reason} {compact(count)}</span>)}
              {!Object.keys(qualityReasons).length ? <span className="text-slate-500">当前没有待分类缺口</span> : null}
            </div>
          </div>
          <div className="rounded-lg border border-white/10 p-3">
            <b className="text-white">下一次补算时间</b>
            <p className="mt-2 text-slate-400">{safeText(coinQuality.next_retry_at, "无待重试项目；增量任务将按到期状态继续检查")}</p>
            <p className="mt-2 text-xs text-slate-500">尚未到期不是错误，unavailable 不等于亏损。</p>
          </div>
        </div>
      </section>
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
            <span>Binance 成交量：{compact(lifecycleMetrics.volume)}</span>
            <span>Binance 报价成交额：{compact(lifecycleMetrics.quote_volume)}</span>
            <span>Binance OI：{pct(lifecycle.lifecycle.oi_change_from_first_pct)}</span>
            <span>合约 CVD：{safeText(lifecycle.lifecycle.futures_cvd_status, "数据不足")}</span>
            <span>现货 CVD：{safeText(lifecycle.lifecycle.spot_cvd_status, "数据不足")}</span>
            <span>资金费率：{safeText(lifecycle.lifecycle.funding_status, "数据不足")}</span>
            <span>旁路观察：其他交易所仅看当前价格和资金费率</span>
            <span>{safeText(lifecycle.lifecycle.not_advice, "仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。")}</span>
          </div>
          <div className="mt-4 rounded-lg border border-white/10 bg-white/[0.03] p-3">
            <h3 className="text-sm font-black text-white">其他交易所旁路观察</h3>
            {exchangeItems.length ? (
              <div className="mt-2 grid gap-2 text-sm text-slate-300 md:grid-cols-2">
                {exchangeItems.map((item, index) => (
                  <div className="rounded-lg border border-white/10 p-2" key={`${safeText(item.exchange, "exchange")}-${index}`}>
                    <b className="text-white">{safeText(item.exchange, "其他交易所")}</b>
                    <p>当前价格 {compact(item.current_price)} · 资金费率 {compact(item.funding_rate)}</p>
                    <p>价格偏离 Binance {pct(item.price_deviation_vs_binance_pct)} · 费率偏离 {compact(item.funding_deviation_vs_binance)}</p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="mt-2 text-sm text-slate-500">暂无旁路行情；其他交易所仅展示当前价格和资金费率，不参与生命周期评分。</p>
            )}
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
