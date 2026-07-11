"use client";

import { useEffect, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { PageTitle } from "@/components/PageTitle";
import {
  getOptimizationReadiness,
  getOptimizationReport,
  getOptimizationScenarios,
  getOptimizationSummary,
  invalidatePublicApiCache
} from "@/lib/api";
import { compact, formatDateTime, pct, ratioPct, safeText } from "@/lib/format";
import type {
  OptimizationComparisonMetric,
  OptimizationFactorChange,
  OptimizationReadinessPayload,
  OptimizationReportPayload,
  OptimizationScenarioItem,
  OptimizationScenariosPayload,
  OptimizationSummaryPayload
} from "@/lib/types";

type UnknownRecord = Record<string, unknown>;

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord)
    : {};
}

function pick(record: UnknownRecord, ...keys: string[]): unknown {
  for (const key of keys) {
    const value = record[key];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return undefined;
}

function listOfText(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (typeof item === "string" && item.trim()) return [item.trim()];
    const record = asRecord(item);
    const text = pick(record, "reason", "label", "message", "recommendation", "text");
    return text === undefined ? [] : [safeText(text)];
  });
}

function hasAutoApplyTrue(value: unknown): boolean {
  if (Array.isArray(value)) return value.some((item) => hasAutoApplyTrue(item));
  const record = asRecord(value);
  return Object.entries(record).some(([key, item]) => (
    (key === "auto_apply" && item === true) || hasAutoApplyTrue(item)
  ));
}

function boolText(value: unknown, fallback = "-"): string {
  if (value === true) return "true";
  if (value === false) return "false";
  return fallback;
}

function modelVersion(value: unknown): string {
  if (typeof value === "string" || typeof value === "number") return safeText(value);
  const model = asRecord(value);
  return safeText(pick(model, "version", "model_version", "name", "id"));
}

function valueText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 }).format(value);
  }
  if (typeof value === "string") return value;
  return safeText(pick(asRecord(value), "value", "label", "name"));
}

function deltaText(value: unknown): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return valueText(value);
  return `${number > 0 ? "+" : ""}${number.toFixed(4).replace(/\.0+$/, "")}`;
}

function ratioText(value: unknown): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return Math.abs(number) > 1 ? pct(number, 1) : ratioPct(number, 1);
}

function scenarioRows(value: unknown): OptimizationScenarioItem[] {
  if (Array.isArray(value)) {
    return value.filter((item): item is OptimizationScenarioItem => Boolean(item) && typeof item === "object");
  }
  return [];
}

function factorRows(value: unknown): OptimizationFactorChange[] {
  if (Array.isArray(value)) {
    return value.filter((item): item is OptimizationFactorChange => Boolean(item) && typeof item === "object");
  }
  return Object.entries(asRecord(value)).map(([key, raw]) => {
    const item = asRecord(raw);
    return {
      factor_key: key,
      factor_label: safeText(pick(item, "factor_label", "label", "name"), key),
      ...item
    } as OptimizationFactorChange;
  });
}

function productionParams(model: UnknownRecord): UnknownRecord {
  const direct = asRecord(pick(model, "params", "parameters"));
  const thresholds = asRecord(model.decision_thresholds);
  const weights = asRecord(model.decision_weights);
  const modules = asRecord(model.module_weights);
  return {
    ...thresholds,
    ...weights,
    ...direct,
    ...Object.fromEntries(Object.entries(modules).map(([key, value]) => [`module_weight_${key}`, value]))
  };
}

function scenarioFactorRows(
  scenario: OptimizationScenarioItem,
  productionModel: UnknownRecord
): OptimizationFactorChange[] {
  // The core's factor_changes contains authoritative, paired old/new values
  // (for example 70 -> 75/80 and Spot/Futures 10/10 -> 15/5). Never
  // reconstruct those pairs from unrelated production metrics.
  const authoritative = factorRows(scenario.factor_changes);
  if (authoritative.length) return authoritative;
  const compatible = factorRows(scenario.parameter_changes || scenario.factors);
  if (compatible.length) return compatible;
  const production = asRecord(
    scenario.production_params ||
    pick(asRecord(scenario.production), "params", "parameters") ||
    productionParams(productionModel)
  );
  const candidate = asRecord(
    scenario.candidate_params || pick(asRecord(scenario.candidate), "params", "parameters")
  );
  return Array.from(new Set([...Object.keys(production), ...Object.keys(candidate)])).map((key) => ({
    factor_key: key,
    factor_label: key,
    old_value: production[key] as OptimizationFactorChange["old_value"],
    new_value: candidate[key] as OptimizationFactorChange["new_value"]
  }));
}

function comparisonRows(
  value: unknown,
  production?: unknown,
  candidate?: unknown,
  delta?: unknown
): OptimizationComparisonMetric[] {
  if (Array.isArray(value)) {
    return value.filter((item): item is OptimizationComparisonMetric => Boolean(item) && typeof item === "object");
  }
  const explicit = asRecord(value);
  if (Object.keys(explicit).length) {
    return Object.entries(explicit).map(([key, raw]) => ({
      metric_key: key,
      ...(asRecord(raw) as OptimizationComparisonMetric)
    }));
  }
  const productionRecord = asRecord(production);
  const candidateRecord = asRecord(candidate);
  const deltaRecord = asRecord(delta);
  const deltaKeys: Record<string, string> = {
    sample_count: "selected_sample_delta",
    success_ratio: "success_ratio_delta",
    decision_accuracy: "decision_accuracy_delta",
    avg_return_pct: "avg_return_delta_pct",
    median_return_pct: "median_return_delta_pct",
    avg_max_gain_pct: "avg_max_gain_delta_pct",
    avg_max_drawdown_pct: "avg_drawdown_improvement_pct",
    drawdown_event_ratio: "drawdown_event_ratio_delta",
    expectancy_pct: "expectancy_delta_pct"
  };
  const keys = Array.from(new Set([
    ...Object.keys(productionRecord),
    ...Object.keys(candidateRecord)
  ])).filter((key) => {
    if (["model_version", "version", "name", "horizons"].includes(key)) return false;
    const left = productionRecord[key];
    const right = candidateRecord[key];
    return [left, right].some((item) => item === null || ["number", "string", "boolean"].includes(typeof item));
  });
  return keys.map((key) => ({
    metric_key: key,
    production: productionRecord[key] as OptimizationComparisonMetric["production"],
    candidate: candidateRecord[key] as OptimizationComparisonMetric["candidate"],
    delta: deltaRecord[deltaKeys[key] || key] as OptimizationComparisonMetric["delta"]
  }));
}

function confidenceText(value: unknown, label?: string): string {
  const record = asRecord(value);
  const score = pick(record, "score", "confidence");
  return safeText(label || record.label, ratioText(score ?? value));
}

function factorName(item: OptimizationFactorChange): string {
  return safeText(
    item.factor_label || item.label || item.factor || item.factor_key,
    "未命名因子"
  );
}

function metricName(item: OptimizationComparisonMetric): string {
  return safeText(item.label || item.metric || item.metric_key, "指标");
}

function FactorChangeTable({ rows }: { rows: OptimizationFactorChange[] }) {
  if (!rows.length) {
    return <EmptyState title="暂无因子改动" text="候选方案尚未提供可比较的参数变化。" />;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-left text-sm text-slate-300">
        <thead className="border-b border-white/10 text-slate-500">
          <tr><th className="px-2 py-2">因子</th><th className="px-2 py-2">old</th><th className="px-2 py-2">new</th><th className="px-2 py-2">delta</th></tr>
        </thead>
        <tbody>
          {rows.map((item, index) => (
            <tr className="border-b border-white/5" key={`${safeText(item.factor_key || item.factor, "factor")}-${index}`}>
              <td className="px-2 py-3 font-bold text-white">{factorName(item)}</td>
              <td className="px-2 py-3">{valueText(item.old_value ?? item.production_value)}</td>
              <td className="px-2 py-3">{valueText(item.new_value ?? item.candidate_value)}</td>
              <td className="px-2 py-3 text-cyan-200">{deltaText(item.delta ?? item.delta_pct)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ComparisonTable({ rows }: { rows: OptimizationComparisonMetric[] }) {
  if (!rows.length) {
    return <EmptyState title="暂无可比较结果" text="生产模型与候选方案仍在生成同口径验证结果。" />;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[620px] text-left text-sm text-slate-300">
        <thead className="border-b border-white/10 text-slate-500">
          <tr><th className="px-2 py-2">指标</th><th className="px-2 py-2">production</th><th className="px-2 py-2">candidate</th><th className="px-2 py-2">delta</th></tr>
        </thead>
        <tbody>
          {rows.map((item, index) => (
            <tr className="border-b border-white/5" key={`${safeText(item.metric_key || item.metric, "metric")}-${index}`}>
              <td className="px-2 py-3 font-bold text-white">{metricName(item)}</td>
              <td className="px-2 py-3">{valueText(item.production ?? item.production_value)}</td>
              <td className="px-2 py-3">{valueText(item.candidate ?? item.candidate_value)}</td>
              <td className="px-2 py-3 text-cyan-200">{deltaText(item.delta ?? item.delta_pct)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function OptimizationPage() {
  const [summary, setSummary] = useState<OptimizationSummaryPayload>({});
  const [scenarios, setScenarios] = useState<OptimizationScenariosPayload>({});
  const [report, setReport] = useState<OptimizationReportPayload>({});
  const [readiness, setReadiness] = useState<OptimizationReadinessPayload>({});
  const [error, setError] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  async function load(refresh = false) {
    if (refresh) invalidatePublicApiCache("/public-api/optimization/");
    setLoading(true);
    setError("");
    const results = await Promise.allSettled([
      getOptimizationSummary(),
      getOptimizationScenarios(),
      getOptimizationReport(),
      getOptimizationReadiness()
    ]);
    const failures: string[] = [];
    const value = <T,>(index: number, fallback: T): T => {
      const result = results[index];
      if (result.status === "fulfilled") return result.value as T;
      failures.push(result.reason instanceof Error ? result.reason.message : "公开模拟数据暂时不可用");
      return fallback;
    };
    setSummary(value(0, {} as OptimizationSummaryPayload));
    setScenarios(value(1, {} as OptimizationScenariosPayload));
    setReport(value(2, {} as OptimizationReportPayload));
    setReadiness(value(3, {} as OptimizationReadinessPayload));
    setWarnings(failures);
    if (failures.length === results.length) setError("模型优化模拟报告暂时不可用，请稍后重试。");
    setLoading(false);
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) return <ErrorState message={error} onRetry={() => load(true)} />;

  const reportData = { ...asRecord(report.report), ...report } as OptimizationReportPayload;
  const summaryData = {
    ...asRecord(reportData.summary),
    ...summary
  } as OptimizationSummaryPayload;
  const readinessData = {
    ...asRecord(reportData.readiness),
    ...readiness
  } as OptimizationReadinessPayload;
  const scenarioDefinitions = [
    scenarioRows(scenarios.items),
    scenarioRows(scenarios.scenarios),
    scenarioRows(reportData.scenarios),
    scenarioRows(reportData.items)
  ].find((items) => items.length) || [];
  const detailedScenarios = [
    scenarioRows(reportData.comparisons),
    scenarioRows(reportData.runs)
  ].find((items) => items.length) || [];
  const detailsByKey = new Map(
    detailedScenarios.map((item) => [safeText(item.scenario_key || item.scenario_id || item.scenario), item])
  );
  const scenarioItems = scenarioDefinitions.map((definition) => ({
    ...definition,
    ...detailsByKey.get(safeText(definition.scenario_key || definition.scenario_id || definition.scenario))
  }));
  for (const detail of detailedScenarios) {
    const key = safeText(detail.scenario_key || detail.scenario_id || detail.scenario);
    if (!scenarioItems.some((item) => safeText(item.scenario_key || item.scenario_id || item.scenario) === key)) {
      scenarioItems.push(detail);
    }
  }
  const production = asRecord(summaryData.production_model || reportData.production_model);
  const productionVersion = modelVersion(
    summaryData.production_model_version || summaryData.production_model ||
    summaryData.model_version || reportData.production_model || summaryData.base_model
  );
  const immutable = summaryData.immutable ?? production.immutable ?? summaryData.does_not_modify_model ?? true;
  const reportRecommendations = Array.isArray(reportData.recommendations) ? reportData.recommendations : [];
  const autoApplyViolation = (
    hasAutoApplyTrue(summaryData) ||
    hasAutoApplyTrue(reportData.auto_apply) ||
    hasAutoApplyTrue(reportRecommendations) ||
    scenarioItems.some((item) => (
      item.auto_apply === true || hasAutoApplyTrue(item.recommendations)
    ))
  );
  const current = asRecord(readinessData.current);
  const required = asRecord(readinessData.required);

  return (
    <div className="space-y-5">
      <PageTitle
        title="模型优化模拟"
        subtitle="在不可变的生产模型之外验证候选因子与历史表现；所有结果均为只读模拟。"
        tags={["production immutable", "auto_apply=false", "人工审核"]}
      />

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-slate-500">报告生成：{formatDateTime(summaryData.generated_at || readinessData.generated_at)}</p>
        <button className="btn" disabled={loading} onClick={() => void load(true)}>
          {loading ? "加载中" : "刷新模拟报告"}
        </button>
      </div>

      {warnings.length ? (
        <div className="rounded-xl border border-amber-400/30 bg-amber-400/5 p-3 text-sm text-amber-100">
          部分只读模拟维度暂时不可用，其余结果仍可查看。
        </div>
      ) : null}
      {autoApplyViolation ? (
        <div className="rounded-xl border border-risk/50 bg-risk/10 p-4 text-sm text-red-100">
          安全边界异常：接口返回了 auto_apply=true。本页面不会执行或提交任何模型变更，请人工复核报告来源。
        </div>
      ) : null}

      <section className="grid gap-4 md:grid-cols-3 xl:grid-cols-6">
        <MetricCard label="生产模型版本" value={productionVersion} />
        <MetricCard label="生产模型 immutable" value={boolText(immutable, "true")} tone="good" />
        <MetricCard label="auto_apply" value="false" tone="good" />
        <MetricCard label="候选方案" value={compact(summaryData.scenario_count ?? scenarioItems.length)} tone="info" />
        <MetricCard label="模拟状态" value={summaryData.status_label || summaryData.status || "只读"} />
        <MetricCard label="验证准备度" value={readinessData.label || readinessData.status || (readinessData.ready ? "已就绪" : "待人工复核")} tone={readinessData.ready ? "good" : "warn"} />
      </section>

      <section className="panel border-cyanline/30 bg-cyanline/5 p-5">
        <h2 className="font-black text-cyan-100">不可变生产边界</h2>
        <p className="mt-2 text-sm leading-6 text-slate-300">
          生产模型保持 <code>immutable=true</code>，候选方案保持 <code>auto_apply=false</code>。此页面不会自动修改模型，也不会调用私有执行接口。
        </p>
        <p className="mt-2 text-xs text-slate-500">仅模拟、不构成投资建议；不生成买入、卖出或自动交易指令。</p>
      </section>

      <section className="panel p-5">
        <div className="mb-4">
          <h2 className="text-lg font-black text-white">候选方案与因子 old / new</h2>
          <p className="mt-1 text-sm text-slate-400">每个方案独立比较生产值、候选值和变化量，所有建议必须经过人工审核。</p>
        </div>
        {scenarioItems.length ? (
          <div className="space-y-5">
            {scenarioItems.map((scenario, index) => {
              const scenarioRecord = scenario as UnknownRecord;
              const factors = scenarioFactorRows(scenario, production);
              const comparisons = comparisonRows(
                scenario.comparisons,
                scenario.production,
                scenario.candidate,
                scenario.delta
              );
              const recommendationRows = listOfText(scenario.recommendations);
              const reasons = listOfText(scenario.reasons);
              const manualReview = scenario.manual_review_required ?? scenario.manual_review ?? true;
              return (
                <article className="rounded-2xl border border-white/10 bg-black/10 p-4" key={safeText(scenario.scenario_id || scenario.scenario_key, String(index))}>
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <h3 className="text-lg font-black text-white">{safeText(scenario.scenario_name || scenario.name || scenario.label || scenario.scenario || scenario.scenario_key, `候选方案 ${index + 1}`)}</h3>
                      <p className="mt-1 text-sm text-slate-400">{safeText(scenario.recommendation || recommendationRows[0] || scenario.description, "等待更多成熟样本后再人工判断。")}</p>
                    </div>
                    <div className="flex flex-wrap gap-2 text-xs">
                      <span className="chip">置信度：{confidenceText(scenario.confidence, scenario.confidence_label)}</span>
                      <span className="chip">人工审核：{manualReview === false ? "仍建议复核" : "必须"}</span>
                      <span className="chip">auto_apply=false</span>
                    </div>
                  </div>
                  <div className="mt-4 grid gap-4 xl:grid-cols-2">
                    <div>
                      <h4 className="mb-2 text-sm font-bold text-slate-200">因子 old / new</h4>
                      <FactorChangeTable rows={factors} />
                    </div>
                    <div>
                      <h4 className="mb-2 text-sm font-bold text-slate-200">production / candidate / delta</h4>
                      <ComparisonTable rows={comparisons} />
                    </div>
                  </div>
                  {reasons.length ? <p className="mt-4 text-sm text-slate-300">原因：{reasons.join("；")}</p> : null}
                  <p className="mt-2 text-xs text-slate-500">
                    状态：{safeText(scenario.status, "模拟")}; 生产模型：{modelVersion(pick(scenarioRecord, "production_model", "base_model")) || productionVersion}
                  </p>
                </article>
              );
            })}
          </div>
        ) : <EmptyState title="暂无候选方案" text="模拟样本仍在积累，不会用空样本生成参数调整结论。" />}
      </section>

      <section className="panel p-5">
        <h2 className="text-lg font-black text-white">建议、原因与人工审核</h2>
        {reportRecommendations.length ? (
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            {reportRecommendations.map((raw, index) => {
              const item = asRecord(raw);
              return (
                <div className="rounded-xl border border-white/10 p-4" key={`${safeText(pick(item, "key", "scenario_id"), "recommendation")}-${index}`}>
                  <h3 className="font-bold text-white">{safeText(pick(item, "label", "name", "recommendation"), `建议 ${index + 1}`)}</h3>
                  <p className="mt-2 text-sm text-slate-300">{safeText(pick(item, "reason", "summary", "description"), "等待人工复核。")}</p>
                  <p className="mt-2 text-xs text-slate-500">置信度：{ratioText(item.confidence)} · 人工审核：必须 · auto_apply=false</p>
                </div>
              );
            })}
          </div>
        ) : <EmptyState title="暂无全局优化建议" text="候选方案可以查看，但不会自动应用或修改生产模型。" />}
      </section>

      <section className={`panel border p-5 ${readinessData.ready ? "border-emerald-400/40" : "border-amber-400/40"}`}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-black text-white">Optimization Readiness</h2>
            <p className="mt-1 text-sm text-slate-400">{safeText(readinessData.label || readinessData.status, "暂未达到人工优化验证条件")}</p>
          </div>
          <span className="chip">{readinessData.ready ? "可进入人工审核" : "继续积累验证样本"}</span>
        </div>
        {Object.keys(current).length || Object.keys(required).length ? (
          <div className="mt-4 grid gap-3 text-sm text-slate-300 md:grid-cols-2">
            <span>当前：{Object.entries(current).map(([key, value]) => `${key}=${valueText(value)}`).join("；") || "-"}</span>
            <span>要求：{Object.entries(required).map(([key, value]) => `${key}=${valueText(value)}`).join("；") || "-"}</span>
          </div>
        ) : null}
        {(readinessData.passed || []).length ? <p className="mt-4 text-sm text-emerald-200">已通过：{(readinessData.passed || []).join("；")}</p> : null}
        {(readinessData.blocked || []).length ? <p className="mt-2 text-sm text-amber-200">阻断项：{(readinessData.blocked || []).join("；")}</p> : null}
        {(readinessData.warnings || []).length ? <p className="mt-2 text-sm text-slate-400">注意：{(readinessData.warnings || []).join("；")}</p> : null}
        <p className="mt-3 text-xs text-slate-500">Readiness 仅表示是否具备人工复核条件；不会自动修改模型，auto_apply=false。</p>
      </section>
    </div>
  );
}
