"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { PageTitle } from "@/components/PageTitle";
import { getLifecycleIntelligenceDetail, getLifecycleReplay, getLifecycleReplayFrames, getLifecycleSimilar, invalidatePublicApiCache } from "@/lib/api";
import { compact, normalizeSymbol, pct, safeText } from "@/lib/format";
import type { LifecycleIntelligenceDetailPayload, LifecycleReplayFrame, LifecycleReplayPayload, LifecycleSimilarityPayload } from "@/lib/types";

export default function LifecycleReplayPage() {
  const [query, setQuery] = useState("");
  const [symbol, setSymbol] = useState("");
  const [replay, setReplay] = useState<LifecycleReplayPayload>({});
  const [frames, setFrames] = useState<LifecycleReplayFrame[]>([]);
  const [intelligence, setIntelligence] = useState<LifecycleIntelligenceDetailPayload>({});
  const [similar, setSimilar] = useState<LifecycleSimilarityPayload>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load(refresh = false, requestedSymbol = query) {
    const normalized = normalizeSymbol(requestedSymbol);
    if (!normalized) {
      setError("请输入 BTC 或 BTCUSDT。");
      return;
    }
    if (refresh) invalidatePublicApiCache();
    setLoading(true);
    setError("");
    try {
      const [summaryPayload, framePayload, intelligencePayload, similarPayload] = await Promise.all([
        getLifecycleReplay(normalized),
        getLifecycleReplayFrames(normalized, { limit: 100 }),
        getLifecycleIntelligenceDetail(normalized),
        getLifecycleSimilar(normalized, 5)
      ]);
      setSymbol(normalized);
      setReplay(summaryPayload);
      setFrames(framePayload.items || []);
      setIntelligence(intelligencePayload);
      setSimilar(similarPayload);
    } catch (err) {
      setError(err instanceof Error ? err.message : "生命周期回放加载失败，请稍后重试。");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const initial = new URLSearchParams(window.location.search).get("symbol") || "";
    if (!initial) return;
    setQuery(initial.toUpperCase());
    void load(false, initial);
    // Read the deep link once; later searches are explicitly user-triggered.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error && symbol) return <ErrorState message={error} onRetry={() => load(true)} />;
  const summary = replay.replay;
  const smart = intelligence.intelligence;

  return (
    <div className="space-y-5">
      <PageTitle
        title="生命周期回放"
        subtitle="按事件时间顺序复盘首次信号、周期升级、资金确认、风险节点与最终结果。仅用于信号整理、研究和风险提示，不构成投资建议，不执行自动交易。"
        tags={["中文时间轴", "预计算回放", "研究用途"]}
      />
      <section className="panel flex flex-col gap-3 p-4 sm:flex-row">
        <input className="input flex-1" value={query} onChange={(event) => setQuery(event.target.value.toUpperCase())} placeholder="搜索 BTC 或 BTCUSDT" />
        <button className="btn" onClick={() => load(true)} disabled={loading}>{loading ? "加载中" : "查看回放"}</button>
        <Link className="btn inline-flex items-center justify-center" href="/lifecycle">返回智能排行</Link>
      </section>
      {error ? <div className="panel border-red-400/30 p-4 text-sm text-red-200">{error}</div> : null}

      {summary ? (
        <>
          <section className="panel p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-xl font-black text-white">{safeText(symbol)} 生命周期摘要</h2>
                <p className="mt-1 text-sm text-cyan-100">首次信号 → 升级路径 {safeText(summary.upgrade_path, "数据积累中")}</p>
              </div>
              <span className="chip">最终结果 {safeText(summary.result_label, "数据不足")}</span>
            </div>
          </section>
          <section className="grid gap-4 md:grid-cols-3 xl:grid-cols-6">
            <MetricCard label="智能评分" value={compact(smart?.intelligence_score)} tone="info" />
            <MetricCard label="最高周期" value={safeText(summary.highest_level, "-")} tone="good" />
            <MetricCard label="事件帧" value={compact(summary.frame_count)} />
            <MetricCard label="最大涨幅" value={pct(summary.max_price_gain_pct)} tone="good" />
            <MetricCard label="最大回撤" value={pct(summary.max_drawdown_pct)} tone="bad" />
            <MetricCard label="最终涨跌" value={pct(summary.final_return_pct)} />
          </section>
          <section className="panel grid gap-3 p-4 text-sm text-slate-300 md:grid-cols-5">
            <span>首次周期 {safeText(summary.first_signal_level, "-")}</span>
            <span>持续时间 {compact(summary.duration_sec)} 秒</span>
            <span>到达 1H {summary.time_to_1h_sec == null ? "数据不足" : `${compact(summary.time_to_1h_sec)} 秒`}</span>
            <span>到达 4H {summary.time_to_4h_sec == null ? "数据不足" : `${compact(summary.time_to_4h_sec)} 秒`}</span>
            <span>到达 24H {summary.time_to_24h_sec == null ? "数据不足" : `${compact(summary.time_to_24h_sec)} 秒`}</span>
          </section>
          <section className="grid gap-4 xl:grid-cols-2">
            <div className="panel p-4">
              <h2 className="font-black text-white">资金确认</h2>
              <p className="mt-2 text-sm text-slate-300">{safeText(smart?.capital_confirmation_label, "资金确认数据仍在积累")}</p>
              <p className="mt-3 text-sm text-slate-400">当前阶段：{safeText(smart?.stage_label, "-")} · 质量：{safeText(smart?.quality_label, "-")} · 风险：{safeText(smart?.risk_label, "-")}</p>
            </div>
            <div className="panel p-4">
              <h2 className="font-black text-white">最终结果与历史参考</h2>
              <p className="mt-2 text-sm text-slate-300">Outcome：{safeText(summary.outcome_status, "数据不足")} · 相似样本 {compact(similar.similar_count)}</p>
              <p className="mt-3 text-sm text-slate-500">{safeText(similar.message || similar.disclaimer, "当前相似样本不足，暂不生成统计结论。")}</p>
              {(similar.samples || []).length ? (
                <div className="mt-3 grid gap-2">
                  {(similar.samples || []).slice(0, 5).map((sample, index) => {
                    const sampleSymbol = safeText(sample.symbol, "-");
                    return (
                      <Link className="rounded-lg border border-white/10 p-2 text-sm text-slate-300 hover:border-cyan-400/40" href={`/lifecycle/replay?symbol=${encodeURIComponent(sampleSymbol)}`} key={`${safeText(sample.lifecycle_id, String(index))}-${sampleSymbol}`}>
                        <b className="text-white">{sampleSymbol}</b>
                        <span className="ml-2">相似度 {compact(sample.similarity_score)}</span>
                        <span className="ml-2">{safeText(sample.upgrade_path, "-")}</span>
                        <span className="ml-2">{safeText(sample.result_label, "数据不足")}</span>
                        <span className="ml-2">收益 {pct(sample.final_return_pct)}</span>
                      </Link>
                    );
                  })}
                </div>
              ) : null}
            </div>
          </section>
          <section>
            <h2 className="mb-3 text-lg font-black text-white">生命周期时间轴</h2>
            <div className="grid gap-3">
              {frames.map((frame) => (
                <article className="panel border-l-4 border-l-cyan-400 p-4" key={`${frame.frame_index}-${frame.event_time}`}>
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <h3 className="font-black text-white">#{compact(frame.frame_index)} {safeText(frame.event_label || frame.event_type, "生命周期事件")}</h3>
                      <p className="text-sm text-slate-500">{safeText(frame.event_time)} · {safeText(frame.state_before, "-")} → {safeText(frame.state_after, "-")}</p>
                    </div>
                    <span className="chip">{safeText(frame.signal_level, "-")}</span>
                  </div>
                  <p className="mt-3 text-sm text-slate-300">{safeText(frame.summary, "该节点暂无补充说明")}</p>
                  <div className="mt-3 grid gap-2 text-sm text-slate-400 md:grid-cols-4">
                    <span>价格 {pct(frame.price_change_from_first_pct)}</span>
                    <span>OI {pct(frame.oi_change_from_first_pct)}</span>
                    <span>Spot CVD {compact(frame.spot_cvd_delta)}</span>
                    <span>Futures CVD {compact(frame.futures_cvd_delta)}</span>
                    <span>Funding {compact(frame.funding_rate)}</span>
                    <span>生命周期评分 {compact(frame.lifecycle_score)}</span>
                    <span>智能评分 {compact(frame.intelligence_score)}</span>
                    <span>风险评分 {compact(frame.risk_score)}</span>
                  </div>
                </article>
              ))}
            </div>
            {!frames.length ? <EmptyState title="回放帧仍在生成" text="当前生命周期已有摘要，但完整事件时间轴仍在积累。" /> : null}
          </section>
        </>
      ) : (
        <EmptyState title="搜索生命周期回放" text="输入 BTC 或 BTCUSDT，查看首次信号、升级路径、时间轴、资金确认和最终结果。" />
      )}
    </div>
  );
}
