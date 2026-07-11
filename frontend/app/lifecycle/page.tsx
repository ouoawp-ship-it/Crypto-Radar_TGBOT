"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { PageTitle } from "@/components/PageTitle";
import {
  getLifecycleAnalytics,
  getLifecycleCalibrationReadiness,
  getLifecycleIntelligenceList,
  getLifecycleIntelligenceSummary,
  getLifecycleOutcomeQualityHorizons,
  getLifecycleOutcomeQualityModules,
  getLifecycleOutcomeQualitySummary,
  getLifecycleOutcomeSummary,
  getLifecycleSummary,
  getLifecycles,
  invalidatePublicApiCache
} from "@/lib/api";
import { compact, moduleLabel, pct, ratioPct, safeText } from "@/lib/format";
import type {
  LifecycleCalibrationReadinessPayload,
  LifecycleIntelligenceItem,
  LifecycleIntelligenceSummaryPayload,
  LifecycleItem,
  LifecycleOutcomeQualityDimensionItem,
  LifecycleOutcomeQualityListPayload,
  LifecycleOutcomeQualitySummaryPayload,
  LifecycleOutcomeSummaryPayload,
  LifecycleSummaryPayload
} from "@/lib/types";

function qualityClass(label?: string) {
  if (["强趋势确认", "高质量启动"].includes(label || "")) return "border-emerald-400/40";
  if (["启动有效", "启动观察"].includes(label || "")) return "border-cyan-400/40";
  if (label === "风险升高") return "border-amber-400/40";
  if (label === "启动失败") return "border-red-400/40";
  return "border-slate-500/30";
}

function riskClass(label?: string) {
  if ((label || "").includes("高")) return "text-red-300";
  if ((label || "").includes("中")) return "text-amber-300";
  return "text-emerald-300";
}

function firstNumber(source: Record<string, unknown>, ...keys: string[]): number {
  for (const key of keys) {
    const value = Number(source[key]);
    if (Number.isFinite(value)) return value;
  }
  return 0;
}

function gapReasonText(value: LifecycleOutcomeQualityDimensionItem["top_gap_reasons"]): string {
  if (Array.isArray(value)) {
    return value.slice(0, 3).map((item) => (
      typeof item === "string" ? item : `${item.reason || "unknown"} ${item.count ?? 0}`
    )).join("、") || "-";
  }
  if (value && typeof value === "object") {
    return Object.entries(value).slice(0, 3).map(([reason, count]) => `${reason} ${count}`).join("、") || "-";
  }
  return "-";
}

export default function LifecyclePage() {
  const [summary, setSummary] = useState<LifecycleSummaryPayload>({});
  const [items, setItems] = useState<LifecycleItem[]>([]);
  const [intelligenceSummary, setIntelligenceSummary] = useState<LifecycleIntelligenceSummaryPayload>({});
  const [outcomeQuality, setOutcomeQuality] = useState<LifecycleOutcomeSummaryPayload>({});
  const [candidateQuality, setCandidateQuality] = useState<LifecycleOutcomeQualitySummaryPayload>({});
  const [qualityModules, setQualityModules] = useState<LifecycleOutcomeQualityListPayload>({});
  const [qualityHorizons, setQualityHorizons] = useState<LifecycleOutcomeQualityListPayload>({});
  const [calibration, setCalibration] = useState<LifecycleCalibrationReadinessPayload>({});
  const [intelligenceItems, setIntelligenceItems] = useState<LifecycleIntelligenceItem[]>([]);
  const [upgradePathItems, setUpgradePathItems] = useState<Array<Record<string, unknown>>>([]);
  const [analyticsSummary, setAnalyticsSummary] = useState<Record<string, unknown>>({});
  const [modelWarnings, setModelWarnings] = useState<string[]>([]);
  const [analyticsStatus, setAnalyticsStatus] = useState("insufficient_data");
  const [symbol, setSymbol] = useState("");
  const [state, setState] = useState("");
  const [level, setLevel] = useState("");
  const [risk, setRisk] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  async function load(refresh = false) {
    if (refresh) invalidatePublicApiCache();
    setLoading(true);
    setError("");
    try {
      const [summaryPayload, listPayload, intelligenceSummaryPayload, intelligenceListPayload, upgradePathPayload, outcomeQualityPayload, candidateQualityPayload, qualityModulesPayload, qualityHorizonsPayload, calibrationPayload] = await Promise.all([
        getLifecycleSummary(),
        getLifecycles({ symbol, state, level, risk, limit: 80 }),
        getLifecycleIntelligenceSummary(),
        getLifecycleIntelligenceList({ symbol, state, level, risk, limit: 80 }),
        getLifecycleAnalytics("upgrade-path"),
        getLifecycleOutcomeSummary().catch(() => ({} as LifecycleOutcomeSummaryPayload)),
        getLifecycleOutcomeQualitySummary().catch(() => ({} as LifecycleOutcomeQualitySummaryPayload)),
        getLifecycleOutcomeQualityModules().catch(() => ({} as LifecycleOutcomeQualityListPayload)),
        getLifecycleOutcomeQualityHorizons().catch(() => ({} as LifecycleOutcomeQualityListPayload)),
        getLifecycleCalibrationReadiness().catch(() => ({} as LifecycleCalibrationReadinessPayload))
      ]);
      setSummary(summaryPayload);
      setItems(listPayload.items || []);
      setIntelligenceSummary(intelligenceSummaryPayload);
      setIntelligenceItems(intelligenceListPayload.items || []);
      setUpgradePathItems(upgradePathPayload.items || []);
      setAnalyticsSummary(upgradePathPayload.summary || {});
      setModelWarnings(upgradePathPayload.model_data_warnings || []);
      setAnalyticsStatus(upgradePathPayload.status || "insufficient_data");
      setOutcomeQuality(outcomeQualityPayload);
      setCandidateQuality(candidateQualityPayload);
      setQualityModules(qualityModulesPayload);
      setQualityHorizons(qualityHorizonsPayload);
      setCalibration(calibrationPayload);
    } catch (err) {
      setError(err instanceof Error ? err.message : "生命周期数据暂时不可用，请稍后重试。");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error && !items.length) return <ErrorState message={error} onRetry={() => load(true)} />;
  const s = summary.summary || {};
  const smart = intelligenceSummary.summary || {};
  const upgradePaths = upgradePathItems.slice(0, 8);
  const outcomeData = { ...(outcomeQuality.summary || {}), ...outcomeQuality } as LifecycleOutcomeSummaryPayload;
  const qualityData = {
    ...outcomeData,
    ...(candidateQuality.summary || {}),
    ...candidateQuality
  } as LifecycleOutcomeQualitySummaryPayload;
  const qualityRecord = {
    ...(qualityData.status_counts || {}),
    ...(qualityData as Record<string, unknown>)
  } as Record<string, unknown>;
  const moduleItems = qualityModules.items || [];
  const horizonItems = qualityHorizons.items || (["1h", "4h", "24h", "72h"].map((horizon) => ({
    horizon,
    ...(qualityData.horizons?.[horizon] || {})
  })) as LifecycleOutcomeQualityDimensionItem[]);
  const horizonCounts = (horizon: string, status: string) => Number(outcomeData.horizons?.[horizon]?.[status as keyof NonNullable<typeof outcomeData.horizons>[string]] || 0);
  const waitingCount = ["1h", "4h", "24h", "72h"].reduce((total, horizon) => total + horizonCounts(horizon, "not_due"), 0);
  const unavailableCount = ["1h", "4h", "24h", "72h"].reduce((total, horizon) => total + horizonCounts(horizon, "unavailable"), 0);

  return (
    <div className="space-y-5">
      <PageTitle
        title="生命周期智能排行"
        subtitle="从首次信号到周期升级、资金确认、风险事件与最终结果，形成可回放、可统计的研究闭环。质量标签是研究标签，不是买卖建议。"
        tags={["智能评分", "历史回放", "不执行自动交易"]}
      />
      <div className="flex flex-wrap gap-2">
        <Link className="btn" href="/lifecycle/replay">打开生命周期回放</Link>
      </div>
      <section className="grid gap-4 md:grid-cols-3 xl:grid-cols-6">
        <MetricCard label="已评价生命周期" value={compact(smart.total_count)} tone="info" />
        <MetricCard label="强趋势确认" value={compact(smart.strong_trend_count)} tone="good" />
        <MetricCard label="高质量启动" value={compact(smart.high_quality_count)} tone="good" />
        <MetricCard label="活跃生命周期" value={compact(s.active_count)} tone="info" />
        <MetricCard label="风险升高" value={compact(smart.risk_count ?? s.risk_warning_count)} tone="warn" />
        <MetricCard label="启动失败" value={compact(smart.failed_count)} tone="bad" />
      </section>

      <section className="panel space-y-4 p-4">
        <div>
          <h2 className="font-black text-white">Lifecycle Outcome 数据质量</h2>
          <p className="mt-1 text-sm text-slate-400">关联覆盖率与数据成熟度分别统计。原“Outcome 关联覆盖率”现明确命名为生命周期关联覆盖率；尚未到期不是错误，ineligible 不进入 Outcome 分母，数据不可用也不等于亏损。</p>
        </div>
        <div className="grid gap-4 md:grid-cols-3 xl:grid-cols-5">
          <MetricCard label="生命周期关联覆盖率" value={ratioPct(qualityData.lifecycle_link_coverage_ratio ?? outcomeData.link_coverage_ratio)} tone="info" />
          <MetricCard label="候选信号关联覆盖率" value={ratioPct(qualityData.candidate_link_coverage_ratio)} tone="info" />
          <MetricCard label="到期候选解决率" value={ratioPct(qualityData.due_resolution_ratio)} tone="good" />
          <MetricCard label="有效 Outcome 成熟率" value={ratioPct(qualityData.usable_outcome_maturity_ratio)} tone="good" />
          <MetricCard label="生命周期成熟率" value={ratioPct(qualityData.lifecycle_maturity_ratio ?? outcomeData.maturity_ratio)} tone="good" />
        </div>
        <div className="grid gap-4 md:grid-cols-3 xl:grid-cols-6">
          <MetricCard label="未分类缺口" value={compact(firstNumber(qualityRecord, "generic_unclassified_count", "generic_no_outcome_row"))} tone="warn" />
          <MetricCard label="合资格待补算" value={compact(firstNumber(qualityRecord, "ready", "ready_count"))} tone="info" />
          <MetricCard label="等待到期" value={compact(firstNumber(qualityRecord, "not_due", "not_due_count") || waitingCount)} tone="info" />
          <MetricCard label="数据不可用" value={compact(firstNumber(qualityRecord, "terminal_unavailable", "unavailable", "unavailable_count") || unavailableCount)} tone="warn" />
          <MetricCard label="可重试错误" value={compact(firstNumber(qualityRecord, "retry_wait", "retryable_count"))} tone="warn" />
          <MetricCard label="真实错误" value={compact(firstNumber(qualityRecord, "real_error_count", "terminal_error", "error_count"))} tone="bad" />
        </div>
        <div className="grid gap-3 text-sm text-slate-300 md:grid-cols-3">
          <div className="rounded-lg border border-white/10 p-3">
            <b className="text-white">生命周期关联覆盖率</b>
            <p className="mt-1 text-slate-400">至少关联一条 Outcome 的生命周期数 / 生命周期总数。当前 {compact(qualityData.linked_lifecycle_count)} / {compact(qualityData.lifecycle_count)}。</p>
          </div>
          <div className="rounded-lg border border-white/10 p-3">
            <b className="text-white">候选信号关联覆盖率</b>
            <p className="mt-1 text-slate-400">已关联的 eligible 候选 / eligible 候选总数；纯汇总、公告等不合资格信号不进入分母。</p>
          </div>
          <div className="rounded-lg border border-white/10 p-3">
            <b className="text-white">成熟与解决</b>
            <p className="mt-1 text-slate-400">not_due 不进入到期分母；unavailable 可以计为已解决，但绝不计作成功或亏损。</p>
          </div>
        </div>
        <div>
          <b className="text-sm text-white">未关联原因</b>
          <div className="mt-2 flex flex-wrap gap-2">
            {Object.entries(qualityData.reasons || {}).map(([reason, count]) => <span className="chip" key={reason}>{reason} {compact(count)}</span>)}
            {!Object.keys(qualityData.reasons || {}).length ? <span className="text-sm text-slate-500">暂无未关联原因</span> : null}
          </div>
        </div>
      </section>

      <section className="panel space-y-4 p-4">
        <div>
          <h2 className="font-black text-white">按模块的数据质量</h2>
          <p className="mt-1 text-sm text-slate-400">按真实信号模块展示候选资格、关联和成熟结果，避免用汇总信号稀释分母。</p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[900px] text-left text-sm text-slate-300">
            <thead className="border-b border-white/10 text-slate-500">
              <tr>
                <th className="px-2 py-2">模块</th><th className="px-2 py-2">候选数</th><th className="px-2 py-2">合资格数</th>
                <th className="px-2 py-2">关联数</th><th className="px-2 py-2">成功数</th><th className="px-2 py-2">不可用数</th>
                <th className="px-2 py-2">关联覆盖率</th><th className="px-2 py-2">成熟率</th><th className="px-2 py-2">主要缺口原因</th>
              </tr>
            </thead>
            <tbody>
              {moduleItems.map((item, index) => (
                <tr className="border-b border-white/5" key={`${item.module || item.key || "module"}-${index}`}>
                  <td className="px-2 py-3 font-bold text-white">{moduleLabel(item.module || item.key)}</td>
                  <td className="px-2 py-3">{compact(item.candidate_count)}</td>
                  <td className="px-2 py-3">{compact(item.eligible_count ?? item.eligible)}</td>
                  <td className="px-2 py-3">{compact(item.linked_count ?? item.linked)}</td>
                  <td className="px-2 py-3">{compact(item.success_count ?? item.success)}</td>
                  <td className="px-2 py-3">{compact(item.unavailable_count ?? item.terminal_unavailable ?? item.unavailable)}</td>
                  <td className="px-2 py-3">{ratioPct(item.link_coverage_ratio)}</td>
                  <td className="px-2 py-3">{ratioPct(item.maturity_ratio)}</td>
                  <td className="max-w-xs px-2 py-3 text-slate-400">{gapReasonText(item.top_gap_reasons)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!moduleItems.length ? <p className="py-5 text-sm text-slate-500">模块数据质量仍在生成，基础生命周期展示不受影响。</p> : null}
        </div>
      </section>

      <section className="panel space-y-4 p-4">
        <div>
          <h2 className="font-black text-white">Horizon 数据质量</h2>
          <p className="mt-1 text-sm text-slate-400">1h / 4h / 24h / 72h 分别统计到期、重试、不可用、成熟率与解决率。</p>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {horizonItems.map((item, index) => (
            <div className="rounded-xl border border-white/10 p-4" key={`${item.horizon || "horizon"}-${index}`}>
              <h3 className="text-lg font-black text-white">{safeText(item.horizon, "-")}</h3>
              <div className="mt-3 grid grid-cols-2 gap-2 text-sm text-slate-300">
                <span>合资格 {compact(item.eligible_count ?? item.eligible)}</span><span>尚未到期 {compact(item.not_due)}</span>
                <span>成功 {compact(item.success)}</span><span>不可用 {compact(item.terminal_unavailable ?? item.unavailable)}</span>
                <span>重试中 {compact(item.retry_wait)}</span><span>错误 {compact(item.terminal_error ?? item.error)}</span>
                <span>成熟率 {ratioPct(item.maturity_ratio)}</span><span>解决率 {ratioPct(item.resolution_ratio)}</span>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className={`panel border p-4 ${calibration.ready ? "border-emerald-400/40" : "border-amber-400/40"}`}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="font-black text-white">模型校准条件</h2>
            <p className="mt-1 text-sm text-slate-400">{safeText(calibration.label, "暂未达到模型校准条件")}</p>
          </div>
          <span className="chip">{calibration.ready ? "已达到准入门槛" : "尚未达到准入门槛"}</span>
        </div>
        <div className="mt-4 grid gap-3 text-sm text-slate-300 md:grid-cols-3">
          <span>24h success：{compact(calibration.current?.success_24h ?? calibration.current?.horizon_24h_success)} / 要求 {compact(calibration.required?.success_24h ?? calibration.required?.min_24h_success)}</span>
          <span>72h success：{compact(calibration.current?.success_72h ?? calibration.current?.horizon_72h_success)} / 要求 {compact(calibration.required?.success_72h ?? calibration.required?.min_72h_success)}</span>
          <span>到期解决率：{ratioPct(calibration.current?.due_resolution_ratio)} / 要求 {ratioPct(calibration.required?.due_resolution_ratio ?? calibration.required?.min_due_resolution_ratio)}</span>
          <span>生命周期成熟率：{ratioPct(calibration.current?.lifecycle_maturity_ratio)} / 要求 {ratioPct(calibration.required?.lifecycle_maturity_ratio ?? calibration.required?.min_lifecycle_maturity_ratio)}</span>
          <span>真实错误率：{ratioPct(calibration.current?.real_error_ratio ?? calibration.current?.error_ratio)} / 上限 {ratioPct(calibration.required?.max_error_ratio)}</span>
          <span>一致性检查：{(calibration.blocked || []).some((item) => ["duplicate_links", "multiple_primary", "orphan_links"].includes(item)) ? "待修复" : "通过"}</span>
        </div>
        {(calibration.blocked || []).length ? <p className="mt-3 text-sm text-amber-200">阻断项：{(calibration.blocked || []).join("；")}</p> : null}
        <p className="mt-3 text-xs text-slate-500">此处仅判断数据是否足够，不会自动修改模型。</p>
      </section>

      <section className="grid gap-4 xl:grid-cols-3">
        <div className="panel p-4">
          <h2 className="font-black text-white">质量分布</h2>
          <div className="mt-3 flex flex-wrap gap-2">
            {(intelligenceSummary.quality_distribution || []).map((item) => <span className="chip" key={item.label}>{safeText(item.label)} {compact(item.count)}</span>)}
          </div>
          {!(intelligenceSummary.quality_distribution || []).length ? <p className="mt-3 text-sm text-slate-500">历史样本仍在积累</p> : null}
        </div>
        <div className="panel p-4">
          <h2 className="font-black text-white">当前阶段分布</h2>
          <div className="mt-3 flex flex-wrap gap-2">
            {(intelligenceSummary.stage_distribution || []).map((item) => <span className="chip" key={item.label}>{safeText(item.label)} {compact(item.count)}</span>)}
          </div>
          {!(intelligenceSummary.stage_distribution || []).length ? <p className="mt-3 text-sm text-slate-500">历史样本仍在积累</p> : null}
        </div>
        <div className="panel p-4">
          <h2 className="font-black text-white">周期升级路径</h2>
          <div className="mt-3 flex flex-wrap gap-2">
            {upgradePaths.map((item, index) => {
              const path = safeText(item.upgrade_path, "unknown");
              return <span className="chip" key={`${path}-${index}`}>{path} · {compact(item.sample_count)}</span>;
            })}
          </div>
          {!upgradePaths.length ? <p className="mt-3 text-sm text-slate-500">历史样本仍在积累</p> : null}
        </div>
      </section>

      <section className="panel p-4">
        <h2 className="font-black text-white">模型诊断</h2>
        <p className="mt-2 text-sm text-slate-300">
          生命周期总数 {compact(analyticsSummary.total_lifecycle_count)} · Outcome 关联 {compact(analyticsSummary.outcome_linked_count)} · 已解析结果 {compact(analyticsSummary.resolved_outcome_count)}
        </p>
        {analyticsStatus !== "ready" ? (
          <p className="mt-3 text-sm text-slate-500">历史样本仍在积累，模型诊断尚未生成。</p>
        ) : modelWarnings.length ? (
          <ul className="mt-3 space-y-1 text-sm text-amber-200">
            {modelWarnings.map((warning) => <li key={warning}>• {warning}</li>)}
          </ul>
        ) : (
          <p className="mt-3 text-sm text-slate-500">当前未发现需要提示的数据质量警告。</p>
        )}
      </section>

      <section className="panel grid gap-3 p-4 md:grid-cols-5">
        <input className="input" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} placeholder="币种，例如 BTCUSDT" />
        <select className="input" value={state} onChange={(event) => setState(event.target.value)}>
          <option value="">全部状态</option>
          <option value="warming">启动观察</option>
          <option value="launching">启动中</option>
          <option value="upgraded_1h">升级到 1H</option>
          <option value="upgraded_4h">升级到 4H</option>
          <option value="trend_confirmed">大周期确认</option>
          <option value="risk_warning">风险升高</option>
          <option value="cooling">短线冷却</option>
          <option value="failed">启动失败</option>
        </select>
        <select className="input" value={level} onChange={(event) => setLevel(event.target.value)}>
          <option value="">全部周期</option>
          <option value="15m">15m</option>
          <option value="1h">1h</option>
          <option value="4h">4h</option>
          <option value="24h">24h</option>
        </select>
        <select className="input" value={risk} onChange={(event) => setRisk(event.target.value)}>
          <option value="">全部风险</option>
          <option value="低">低风险</option>
          <option value="中">中风险</option>
          <option value="高">高风险</option>
        </select>
        <button className="btn" onClick={() => load(true)} disabled={loading}>
          {loading ? "加载中" : "筛选"}
        </button>
      </section>

      <section>
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-lg font-black text-white">生命周期智能排行</h2>
          <span className="text-sm text-slate-500">按智能评分降序</span>
        </div>
        <div className="grid gap-3 xl:grid-cols-2">
          {intelligenceItems.map((item) => (
            <Link className={`signal-card block ${qualityClass(item.quality_label)}`} href={`/coin/${encodeURIComponent(item.symbol || "")}`} key={`smart-${item.lifecycle_id || item.symbol}`}>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="text-lg font-black text-white">{safeText(item.symbol)}</h3>
                  <p className="text-sm text-cyan-100">{safeText(item.quality_label, "历史样本仍在积累")} · 当前阶段 {safeText(item.stage_label, "-")}</p>
                </div>
                <div className="flex gap-2"><span className="chip">智能评分 {compact(item.intelligence_score)}</span><span className={`chip ${riskClass(item.risk_label)}`}>风险评分 {compact(item.risk_score)}</span></div>
              </div>
              <div className="mt-3 grid gap-2 text-sm text-slate-300 md:grid-cols-3">
                <span>首次周期 {safeText(item.first_signal_level, "-")}</span>
                <span>最高周期 {safeText(item.highest_level, "-")}</span>
                <span>升级路径 {safeText(item.upgrade_path, "-")}</span>
                <span>生命周期评分 {compact(item.lifecycle_score)}</span>
                <span>价格变化 {pct(item.price_change_from_first_pct)}</span>
                <span>OI 变化 {pct(item.oi_change_from_first_pct)}</span>
                <span className="md:col-span-2">资金确认 {safeText(item.capital_confirmation_label, "数据不足")}</span>
                <span>历史相似样本 {compact(item.similar_count)}</span>
              </div>
            </Link>
          ))}
        </div>
        {!intelligenceItems.length && !loading ? <EmptyState title="历史样本仍在积累" text="当前智能评价尚未生成，基础生命周期数据仍可继续查看。" /> : null}
      </section>

      <h2 className="text-lg font-black text-white">基础生命周期列表</h2>
      <section className="grid gap-3 xl:grid-cols-2">
        {items.map((item) => (
          <Link className="signal-card block" href={`/coin/${encodeURIComponent(item.symbol || "")}`} key={item.symbol}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-black text-white">{safeText(item.symbol)}</h2>
                <p className="text-sm text-slate-400">{safeText(item.state_label || item.current_state, "启动观察")} · 首信号 {safeText(item.first_signal_level, "-")} · 最高周期 {safeText(item.highest_level, "-")}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <span className="chip">强度 {compact(item.lifecycle_score)}</span>
                <span className="chip">风险 {compact(item.risk_score)}</span>
              </div>
            </div>
            <div className="mt-3 grid gap-2 text-sm text-slate-300 md:grid-cols-3">
              <span>价格 {pct(item.price_change_from_first_pct)}</span>
              <span>OI {pct(item.oi_change_from_first_pct)}</span>
              <span>资金费率 {safeText(item.funding_status, "数据不足")}</span>
              <span>合约 CVD {safeText(item.futures_cvd_status, "数据不足")}</span>
              <span>现货 CVD {safeText(item.spot_cvd_status, "数据不足")}</span>
              <span>首次信号 {safeText(item.first_signal_at, "-")}</span>
            </div>
            <p className="mt-3 text-xs text-slate-500">{safeText(item.not_advice, "仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。")}</p>
          </Link>
        ))}
      </section>
      {!items.length && !loading ? <EmptyState title="暂无生命周期数据" text="一个币首次出现有效信号后会自动创建生命周期档案。" /> : null}
    </div>
  );
}
