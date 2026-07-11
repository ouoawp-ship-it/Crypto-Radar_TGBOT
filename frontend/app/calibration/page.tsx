"use client";

import { useEffect, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { PageTitle } from "@/components/PageTitle";
import {
  getCalibrationDecision,
  getCalibrationFactors,
  getCalibrationLifecycle,
  getCalibrationReadiness,
  getCalibrationRisk,
  getCalibrationSummary,
  invalidatePublicApiCache
} from "@/lib/api";
import { compact, formatDateTime, pct, ratioPct, safeText } from "@/lib/format";
import type {
  CalibrationMetricItem,
  CalibrationReadinessPayload,
  CalibrationSectionPayload,
  CalibrationSummaryPayload
} from "@/lib/types";

type UnknownRecord = Record<string, unknown>;

const LEVELS = ["15m", "1h", "4h", "24h"];
const FACTOR_GROUPS = [
  { keys: ["oi_quadrants", "oi"], label: "OI" },
  { keys: ["spot_futures_cvd", "spot_cvd"], label: "Spot CVD" },
  { keys: ["futures_cvd", "spot_futures_cvd"], label: "Futures CVD" },
  { keys: ["volume"], label: "Volume" },
  { keys: ["funding"], label: "Funding" }
];

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord)
    : {};
}

function metricRows(value: unknown): CalibrationMetricItem[] {
  if (Array.isArray(value)) {
    return value.filter((item): item is CalibrationMetricItem => Boolean(item) && typeof item === "object");
  }
  const record = asRecord(value);
  if (!Object.keys(record).length) return [];
  const looksLikeMetric = [
    "metric_key", "sample_count", "mature_sample_count", "success_count",
    "success_ratio", "avg_return_pct", "alert_count"
  ].some((key) => key in record);
  if (looksLikeMetric) return [record as CalibrationMetricItem];
  return Object.entries(record).flatMap(([key, item]) => {
    if (!item || typeof item !== "object") return [];
    return metricRows(item).map((row) => ({ metric_key: row.metric_key || key, ...row }));
  });
}

function sectionRows(payload: CalibrationSectionPayload, keys: string[]): CalibrationMetricItem[] {
  for (const key of ["items", ...keys]) {
    const rows = metricRows(payload[key]);
    if (rows.length) return rows;
  }
  return metricRows(payload);
}

function pick(item: UnknownRecord, ...keys: string[]): unknown {
  for (const key of keys) {
    const value = item[key];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return undefined;
}

function ratioText(value: unknown): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return Math.abs(number) > 1 ? pct(number, 1) : ratioPct(number, 1);
}

function countText(item: CalibrationMetricItem): string {
  return compact(pick(item, "sample_count", "total_count", "count"));
}

function returnText(item: CalibrationMetricItem): string {
  return pct(pick(item, "avg_return_pct", "avg_final_return_pct"));
}

function drawdownText(item: CalibrationMetricItem): string {
  return pct(pick(item, "avg_max_drawdown_pct", "avg_drawdown_pct"));
}

function successText(item: CalibrationMetricItem): string {
  return ratioText(pick(item, "success_ratio", "success_rate", "positive_ratio"));
}

function leadTimeText(item: CalibrationMetricItem): string {
  const minutes = Number(pick(item, "avg_lead_time_min", "lead_time_min"));
  if (Number.isFinite(minutes)) return `${minutes.toFixed(1)} 分钟`;
  const seconds = Number(pick(item, "avg_lead_time_sec", "lead_time_sec"));
  return Number.isFinite(seconds) ? `${(seconds / 60).toFixed(1)} 分钟` : "-";
}

function itemLabel(item: CalibrationMetricItem, fallback = "-"): string {
  return safeText(pick(
    item,
    "decision_label", "factor_label", "risk_label", "label", "metric_key",
    "decision_code", "factor", "risk_type", "key"
  ), fallback);
}

function lifecycleLevel(item: CalibrationMetricItem): string {
  return safeText(pick(item, "first_signal_level", "timeframe", "metric_key", "key"), "unknown").toLowerCase();
}

function CalibrationTable({ rows }: { rows: CalibrationMetricItem[] }) {
  if (!rows.length) {
    return <EmptyState title="校准样本仍在积累" text="只有已关联、已到期且成功计算的 Outcome 才进入验证统计。" />;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[760px] text-left text-sm text-slate-300">
        <thead className="border-b border-white/10 text-slate-500">
          <tr>
            <th className="px-2 py-2">决策</th>
            <th className="px-2 py-2">成熟样本</th>
            <th className="px-2 py-2">成功率</th>
            <th className="px-2 py-2">平均收益</th>
            <th className="px-2 py-2">平均回撤</th>
            <th className="px-2 py-2">期望收益</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item, index) => (
            <tr className="border-b border-white/5" key={`${safeText(item.metric_key || item.decision_code, "decision")}-${index}`}>
              <td className="px-2 py-3 font-bold text-white">{itemLabel(item, "未分类决策")}</td>
              <td className="px-2 py-3">{compact(pick(item, "mature_sample_count", "sample_count", "count"))}</td>
              <td className="px-2 py-3">{successText(item)}</td>
              <td className="px-2 py-3">{returnText(item)}</td>
              <td className="px-2 py-3">{drawdownText(item)}</td>
              <td className="px-2 py-3">{pct(item.expectancy_pct)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function CalibrationPage() {
  const [summary, setSummary] = useState<CalibrationSummaryPayload>({});
  const [decision, setDecision] = useState<CalibrationSectionPayload>({});
  const [lifecycle, setLifecycle] = useState<CalibrationSectionPayload>({});
  const [factors, setFactors] = useState<CalibrationSectionPayload>({});
  const [risk, setRisk] = useState<CalibrationSectionPayload>({});
  const [readiness, setReadiness] = useState<CalibrationReadinessPayload>({});
  const [error, setError] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  async function load(refresh = false) {
    if (refresh) invalidatePublicApiCache("/public-api/calibration/");
    setLoading(true);
    setError("");
    const results = await Promise.allSettled([
      getCalibrationSummary(),
      getCalibrationDecision(),
      getCalibrationLifecycle(),
      getCalibrationFactors(),
      getCalibrationRisk(),
      getCalibrationReadiness()
    ]);
    const failures: string[] = [];
    const value = <T,>(index: number, fallback: T): T => {
      const result = results[index];
      if (result.status === "fulfilled") return result.value as T;
      failures.push(result.reason instanceof Error ? result.reason.message : "公开校准数据暂时不可用");
      return fallback;
    };
    setSummary(value(0, {} as CalibrationSummaryPayload));
    setDecision(value(1, {} as CalibrationSectionPayload));
    setLifecycle(value(2, {} as CalibrationSectionPayload));
    setFactors(value(3, {} as CalibrationSectionPayload));
    setRisk(value(4, {} as CalibrationSectionPayload));
    setReadiness(value(5, {} as CalibrationReadinessPayload));
    setWarnings(failures);
    if (failures.length === results.length) setError("模型校准报告暂时不可用，请稍后重试。");
    setLoading(false);
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) return <ErrorState message={error} onRetry={() => load(true)} />;

  const summaryData = { ...asRecord(summary.summary), ...summary } as CalibrationSummaryPayload;
  const readinessData = {
    ...asRecord(readiness.readiness),
    ...readiness
  } as CalibrationReadinessPayload;
  const decisionRows = sectionRows(decision, ["decision_labels", "decision"]);
  const lifecycleRows = sectionRows(lifecycle, ["first_levels", "lifecycle"]);
  const levelRows = LEVELS.map((level) => (
    lifecycleRows.find((item) => lifecycleLevel(item) === level) || { metric_key: level, label: level }
  ));
  const factorSource = asRecord(factors.factors || factors);
  const factorRows = FACTOR_GROUPS.flatMap((group) => {
    const sourceKey = group.keys.find((key) => key in factorSource);
    if (!sourceKey) return [{ metric_key: group.keys[0], factor_label: group.label }];
    const rows = metricRows(factorSource[sourceKey]);
    return rows.length
      ? rows.map((item) => ({ ...item, factor_label: group.label }))
      : [{ metric_key: sourceKey, factor_label: group.label }];
  });
  const riskRows = sectionRows(risk, ["risk_alerts", "risk"]);
  const current = asRecord(readinessData.current);
  const required = asRecord(readinessData.required);
  const statusLabel = safeText(
    summaryData.status_label || summaryData.label || readinessData.label || summaryData.status,
    loading ? "加载中" : "样本积累中"
  );

  return (
    <div className="space-y-5">
      <PageTitle
        title="模型校准"
        subtitle="基于已关联、已到期且成功计算的 Outcome，验证 Decision、生命周期周期、资金因子与风险警报的历史表现。"
        tags={["只读验证", "闭环样本", "不执行自动交易"]}
      />

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-slate-500">报告生成：{formatDateTime(summaryData.generated_at || readinessData.generated_at || readinessData.calculated_at)}</p>
        <button className="btn" disabled={loading} onClick={() => void load(true)}>
          {loading ? "加载中" : "刷新校准报告"}
        </button>
      </div>

      {warnings.length ? (
        <div className="rounded-xl border border-amber-400/30 bg-amber-400/5 p-3 text-sm text-amber-100">
          部分校准维度暂时不可用，其余只读结果仍可查看。
        </div>
      ) : null}

      <section className="grid gap-4 md:grid-cols-3 xl:grid-cols-6">
        <MetricCard label="模型版本" value={summaryData.model_version || summaryData.calibration_version || "-"} />
        <MetricCard label="总样本" value={compact(pick(summaryData, "sample_count", "total_samples", "total_count"))} />
        <MetricCard label="成熟样本" value={compact(pick(summaryData, "mature_sample_count", "mature_samples"))} tone="good" />
        <MetricCard label="样本成熟率" value={ratioText(summaryData.maturity_ratio)} tone="info" />
        <MetricCard label="不可用样本" value={compact(summaryData.unavailable_count)} tone="warn" />
        <MetricCard label="校准状态" value={statusLabel} tone={readinessData.ready ? "good" : "warn"} />
      </section>

      <section className="panel border-cyanline/30 bg-cyanline/5 p-5">
        <h2 className="font-black text-cyan-100">只读校准说明</h2>
        <p className="mt-2 text-sm leading-6 text-slate-300">
          此页面仅验证历史样本是否支持当前模型规则，不会自动修改 Decision Model 阈值或 Lifecycle Intelligence 权重，也不会生成买入、卖出或自动交易指令。
        </p>
        <p className="mt-2 text-xs text-slate-500">仅用于信号整理、模型研究和风险提示，不构成投资建议，不执行自动交易。</p>
      </section>

      <section className="panel p-5">
        <div className="mb-4">
          <h2 className="text-lg font-black text-white">Decision 校准表现</h2>
          <p className="mt-1 text-sm text-slate-400">按决策标签比较成熟样本、成功率、平均收益和平均回撤。</p>
        </div>
        <CalibrationTable rows={decisionRows} />
      </section>

      <section className="panel p-5">
        <div className="mb-4">
          <h2 className="text-lg font-black text-white">生命周期周期表现</h2>
          <p className="mt-1 text-sm text-slate-400">比较 15m / 1h / 4h / 24h 首信号的历史成熟表现。</p>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {levelRows.map((item) => (
            <div className="rounded-xl border border-white/10 p-4" key={lifecycleLevel(item)}>
              <h3 className="text-lg font-black text-white">{lifecycleLevel(item)}</h3>
              <div className="mt-3 grid gap-2 text-sm text-slate-300">
                <span>成熟样本：{compact(pick(item, "mature_sample_count", "sample_count"))}</span>
                <span>成功率：{successText(item)}</span>
                <span>平均收益：{returnText(item)}</span>
                <span>平均回撤：{drawdownText(item)}</span>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="panel p-5">
        <div className="mb-4">
          <h2 className="text-lg font-black text-white">资金与市场因子验证</h2>
          <p className="mt-1 text-sm text-slate-400">OI、Spot CVD、Futures CVD、Volume 与 Funding 只用于验证历史确认效果。</p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-left text-sm text-slate-300">
            <thead className="border-b border-white/10 text-slate-500">
              <tr><th className="px-2 py-2">因子</th><th className="px-2 py-2">分组</th><th className="px-2 py-2">样本</th><th className="px-2 py-2">成功率</th><th className="px-2 py-2">平均收益</th><th className="px-2 py-2">结论</th></tr>
            </thead>
            <tbody>
              {factorRows.map((item, index) => (
                <tr className="border-b border-white/5" key={`${safeText(item.factor_label, "factor")}-${safeText(item.metric_key, String(index))}-${index}`}>
                  <td className="px-2 py-3 font-bold text-white">{safeText(item.factor_label, "因子")}</td>
                  <td className="px-2 py-3">{safeText(pick(item, "label", "metric_key", "key", "factor"), "全部")}</td>
                  <td className="px-2 py-3">{countText(item)}</td>
                  <td className="px-2 py-3">{successText(item)}</td>
                  <td className="px-2 py-3">{returnText(item)}</td>
                  <td className="max-w-sm px-2 py-3 text-slate-400">{safeText(item.conclusion || item.status, "样本仍在积累")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel p-5">
        <div className="mb-4">
          <h2 className="text-lg font-black text-white">风险警报有效性</h2>
          <p className="mt-1 text-sm text-slate-400">验证风险警报次数、提前量、警报后平均回撤与有效率，不产生交易指令。</p>
        </div>
        {riskRows.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[700px] text-left text-sm text-slate-300">
              <thead className="border-b border-white/10 text-slate-500">
                <tr><th className="px-2 py-2">风险事件</th><th className="px-2 py-2">警报次数</th><th className="px-2 py-2">平均提前量</th><th className="px-2 py-2">警报后平均回撤</th><th className="px-2 py-2">有效率</th></tr>
              </thead>
              <tbody>
                {riskRows.map((item, index) => (
                  <tr className="border-b border-white/5" key={`${safeText(item.metric_key || item.risk_type, "risk")}-${index}`}>
                    <td className="px-2 py-3 font-bold text-white">{itemLabel(item, "风险警报")}</td>
                    <td className="px-2 py-3">{compact(pick(item, "event_count", "alert_count", "sample_count", "count"))}</td>
                    <td className="px-2 py-3">{leadTimeText(item)}</td>
                    <td className="px-2 py-3">{drawdownText(item)}</td>
                    <td className="px-2 py-3">{ratioText(pick(item, "avoided_loss_ratio", "effectiveness_ratio", "success_ratio", "hit_ratio"))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <EmptyState title="风险样本仍在积累" text="不会用空样本展示虚假的 0% 有效率。" />}
      </section>

      <section className={`panel border p-5 ${readinessData.ready ? "border-emerald-400/40" : "border-amber-400/40"}`}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-black text-white">模型校准准入</h2>
            <p className="mt-1 text-sm text-slate-400">{safeText(readinessData.label || readinessData.status, "暂未达到模型校准条件")}</p>
          </div>
          <span className="chip">{readinessData.ready ? "数据已达到验证门槛" : "数据尚未达到验证门槛"}</span>
        </div>
        <div className="mt-4 grid gap-3 text-sm text-slate-300 md:grid-cols-2 xl:grid-cols-4">
          <span>24h success：{compact(pick(current, "success_24h", "horizon_24h_success"))} / {compact(pick(required, "success_24h", "min_24h_success"))}</span>
          <span>72h success：{compact(pick(current, "success_72h", "horizon_72h_success"))} / {compact(pick(required, "success_72h", "min_72h_success"))}</span>
          <span>到期解决率：{ratioText(current.due_resolution_ratio)} / {ratioText(pick(required, "due_resolution_ratio", "min_due_resolution_ratio"))}</span>
          <span>生命周期成熟率：{ratioText(current.lifecycle_maturity_ratio)} / {ratioText(pick(required, "lifecycle_maturity_ratio", "min_lifecycle_maturity_ratio"))}</span>
        </div>
        {(readinessData.passed || []).length ? <p className="mt-4 text-sm text-emerald-200">已通过：{(readinessData.passed || []).join("；")}</p> : null}
        {(readinessData.blocked || []).length ? <p className="mt-2 text-sm text-amber-200">阻断项：{(readinessData.blocked || []).join("；")}</p> : null}
        {(readinessData.warnings || []).length ? <p className="mt-2 text-sm text-slate-400">注意：{(readinessData.warnings || []).join("；")}</p> : null}
        <p className="mt-3 text-xs text-slate-500">此处只判断是否具备模型校准验证条件，不会自动调整任何模型参数。</p>
      </section>
    </div>
  );
}
