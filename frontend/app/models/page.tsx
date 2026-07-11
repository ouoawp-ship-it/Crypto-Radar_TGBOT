"use client";

import { useEffect, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { PageTitle } from "@/components/PageTitle";
import {
  getCurrentModel,
  getModelHealth,
  getModelHistory,
  getModelPerformance,
  invalidatePublicApiCache
} from "@/lib/api";
import {
  getPrivateModelDiff,
  getPrivateModelList,
  submitModelRegistryJob
} from "@/lib/modelRegistryAdmin";
import { compact, formatDateTime, numberText, pct, ratioPct, safeText } from "@/lib/format";
import type {
  ModelCurrentPayload,
  ModelDiffChange,
  ModelDiffPayload,
  ModelHealthAlert,
  ModelHealthPayload,
  ModelHistoryPayload,
  ModelPerformancePayload,
  ModelPerformancePeriod,
  PrivateModelItem,
  PublicModelItem
} from "@/lib/types";

type UnknownRecord = Record<string, unknown>;
const REGISTRY_MODEL_KEYS = [
  "signal-decision",
  "lifecycle-risk",
  "lifecycle-intelligence",
  "simulation-policy"
] as const;

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : {};
}

function modelRows(value: unknown): PublicModelItem[] {
  return Array.isArray(value)
    ? value.filter((item): item is PublicModelItem => Boolean(item) && typeof item === "object")
    : [];
}

function privateModelRows(value: unknown): PrivateModelItem[] {
  return Array.isArray(value)
    ? value.filter((item): item is PrivateModelItem => Boolean(item) && typeof item === "object")
    : [];
}

function currentRow(payload: ModelCurrentPayload): PublicModelItem {
  return payload.current || payload.model || payload;
}

function historyRows(payload: ModelHistoryPayload): PublicModelItem[] {
  return modelRows(payload.items || payload.models);
}

function performanceRows(payload: ModelPerformancePayload): ModelPerformancePeriod[] {
  return (payload.periods || payload.items || payload.snapshots || []).filter(Boolean);
}

function modelKey(item: PublicModelItem): string {
  return safeText(item.model_key, "signal-decision");
}

function modelVersion(item: PublicModelItem): string {
  return safeText(item.model_version, "-");
}

function selectedKey(item: PublicModelItem): string {
  return `${modelKey(item)}:${modelVersion(item)}`;
}

function ratioText(value: unknown): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "-";
  return Math.abs(numeric) > 1 ? pct(numeric, 1) : ratioPct(numeric, 1);
}

function valueText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") return numberText(value, 4);
  if (typeof value === "string") return value;
  return safeText(asRecord(value).label || asRecord(value).summary, "-");
}

function healthTone(status: unknown): "good" | "warn" | "bad" | "neutral" {
  const value = safeText(status, "").toLowerCase();
  if (value === "healthy") return "good";
  if (value === "degraded") return "bad";
  if (value === "warning") return "warn";
  return "neutral";
}

function healthLabel(status: unknown): string {
  const value = safeText(status, "unknown");
  const labels: Record<string, string> = {
    healthy: "健康",
    warning: "预警",
    degraded: "表现退化",
    deprecated: "已停用",
    insufficient_performance_samples: "表现样本不足",
    insufficient_success_baseline: "成功率基线不足",
    unknown: "待评估"
  };
  return labels[value] || value;
}

function statusLabel(status: unknown): string {
  const value = safeText(status, "unknown");
  const labels: Record<string, string> = {
    draft: "草稿",
    simulation: "模拟完成",
    pending: "待批准",
    approved: "已批准",
    production: "生产中",
    deprecated: "已停用",
    rejected: "已拒绝",
    rollback: "已回滚"
  };
  return labels[value] || value;
}

function alertText(alert: string | ModelHealthAlert): string {
  if (typeof alert === "string") {
    const labels: Record<string, string> = {
      insufficient_samples: "成熟表现样本不足，继续观察。",
      insufficient_baseline: "成功率基线不足，暂不判断模型退化。",
      "30d_success_ratio_declined_more_than_15_percent": "近 30 天成功率较基线下降超过 15%。",
      "30d_success_ratio_declined_more_than_30_percent": "近 30 天成功率较基线下降超过 30%。"
    };
    return labels[alert] || alert;
  }
  return safeText(alert.message || alert.label || alert.code, "模型健康提醒");
}

function changeRows(payload: ModelDiffPayload): ModelDiffChange[] {
  if (Array.isArray(payload.changes)) return payload.changes;
  const nested = asRecord(payload.diff);
  return Array.isArray(nested.changes)
    ? nested.changes.filter((item): item is ModelDiffChange => Boolean(item) && typeof item === "object")
    : [];
}

function simulationSummary(item: PrivateModelItem | ModelDiffPayload): UnknownRecord {
  const direct = asRecord(item.simulation_summary);
  if (Object.keys(direct).length) return direct;
  const simulation = asRecord(item.simulation);
  const summary = asRecord(simulation.summary);
  if (Object.keys(summary).length) return summary;
  const metadataSimulation = asRecord(asRecord(item.metadata).simulation);
  return Object.keys(metadataSimulation).length ? metadataSimulation : simulation;
}

function simulationStatus(item: PrivateModelItem): string {
  const metadata = asRecord(item.metadata);
  const simulation = asRecord(metadata.simulation);
  return safeText(item.simulation_status || simulation.status, "待查看");
}

function ModelPerformanceSummary({ value }: { value?: Record<string, unknown> }) {
  const summary = asRecord(value);
  if (!Object.keys(summary).length) return <span className="text-slate-500">暂无成熟表现</span>;
  return (
    <span className="text-slate-300">
      样本 {compact(summary.sample_count ?? summary.mature_sample_count)} · 成功率 {ratioText(summary.success_ratio)} · 平均收益 {pct(summary.avg_return ?? summary.avg_return_pct)}
    </span>
  );
}

export default function ModelsPage() {
  const [current, setCurrent] = useState<ModelCurrentPayload>({});
  const [history, setHistory] = useState<ModelHistoryPayload>({});
  const [performance, setPerformance] = useState<ModelPerformancePayload>({});
  const [health, setHealth] = useState<ModelHealthPayload>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);

  const [privateModels, setPrivateModels] = useState<PrivateModelItem[]>([]);
  const [adminStatus, setAdminStatus] = useState("候选详情和审批操作需要后台登录。");
  const [adminLoading, setAdminLoading] = useState(false);
  const [selected, setSelected] = useState<PrivateModelItem | PublicModelItem | null>(null);
  const [diff, setDiff] = useState<ModelDiffPayload>({});
  const [reason, setReason] = useState("");
  const [jobMessage, setJobMessage] = useState("");

  async function loadPublic(refresh = false) {
    if (refresh) invalidatePublicApiCache("/public-api/models/");
    setLoading(true);
    setError("");
    const results = await Promise.allSettled([
      getCurrentModel(),
      getModelHistory(),
      getModelPerformance(),
      getModelHealth()
    ]);
    const failures: string[] = [];
    const value = <T,>(index: number, fallback: T): T => {
      const result = results[index];
      if (result.status === "fulfilled") return result.value as T;
      failures.push(result.reason instanceof Error ? result.reason.message : "公开模型数据暂时不可用");
      return fallback;
    };
    setCurrent(value(0, {} as ModelCurrentPayload));
    setHistory(value(1, {} as ModelHistoryPayload));
    setPerformance(value(2, {} as ModelPerformancePayload));
    setHealth(value(3, {} as ModelHealthPayload));
    setWarnings(failures);
    if (failures.length === results.length) setError("模型注册信息暂时不可用，请稍后重试。");
    setLoading(false);
  }

  useEffect(() => {
    void loadPublic();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function loadPrivateModels() {
    setAdminLoading(true);
    setJobMessage("");
    try {
      const payloads = await Promise.all(REGISTRY_MODEL_KEYS.map((model) => getPrivateModelList(model)));
      const byVersion = new Map<string, PrivateModelItem>();
      for (const payload of payloads) {
        for (const item of privateModelRows(payload.items || payload.models)) {
          byVersion.set(selectedKey(item), item);
        }
      }
      const items = Array.from(byVersion.values());
      setPrivateModels(items);
      setAdminStatus(items.length ? `已加载 ${items.length} 个后台模型版本。` : "后台已登录，但暂无候选模型。" );
    } catch (caught) {
      setPrivateModels([]);
      setAdminStatus(caught instanceof Error ? caught.message : "后台模型列表加载失败。");
    } finally {
      setAdminLoading(false);
    }
  }

  async function selectForReview(item: PrivateModelItem | PublicModelItem) {
    setSelected(item);
    setDiff({});
    setJobMessage("");
    setAdminLoading(true);
    try {
      const payload = await getPrivateModelDiff(modelKey(item), modelVersion(item));
      setDiff(payload);
      setAdminStatus("已加载受保护的 Production / Candidate 差异摘要。");
    } catch (caught) {
      setAdminStatus(caught instanceof Error ? caught.message : "模型差异加载失败。");
    } finally {
      setAdminLoading(false);
    }
  }

  async function submitAction(action: "approve" | "activate" | "reject" | "rollback") {
    if (!selected) {
      setJobMessage("请先选择一个候选或历史模型版本。");
      return;
    }
    if (reason.trim().length < 4) {
      setJobMessage("请填写至少 4 个字符的人工审批原因。所有决定都会写入审计记录。");
      return;
    }
    const labels = {
      approve: "批准（仅记录批准，不启用）",
      activate: "人工启用为 Production",
      reject: "拒绝候选",
      rollback: "回滚 Production"
    };
    const warning = action === "activate" || action === "rollback"
      ? "该操作会提交生产版本变更任务，但仍需服务端校验和人工部署核验。"
      : "该操作只记录审批结论，不会修改当前 Production。";
    if (!window.confirm(`确认${labels[action]} ${modelKey(selected)} ${modelVersion(selected)}？\n\n${warning}`)) return;

    setAdminLoading(true);
    setJobMessage("");
    try {
      const endpointAction = action === "activate" ? "approve" : action;
      const payload = await submitModelRegistryJob(endpointAction, {
        model: modelKey(selected),
        version: modelVersion(selected),
        reason: reason.trim(),
        activate: action === "activate",
        confirm_production: action === "activate" || action === "rollback"
      });
      setJobMessage(`人工操作已提交为后台任务，job_id=${safeText(payload.job_id)}。页面不会直接修改模型。`);
      setReason("");
      await loadPrivateModels();
    } catch (caught) {
      setJobMessage(caught instanceof Error ? caught.message : "人工操作提交失败，当前模型未改变。");
    } finally {
      setAdminLoading(false);
    }
  }

  if (error) return <ErrorState message={error} onRetry={() => loadPublic(true)} />;

  const production = currentRow(current);
  const histories = historyRows(history);
  const snapshots = performanceRows(performance);
  const candidates = privateModels.filter((item) => ["draft", "simulation", "pending", "approved", "rejected"].includes(safeText(item.status)));
  const selectedChanges = changeRows(diff);
  const selectedSimulation = {
    ...simulationSummary(selected || {}),
    ...simulationSummary(diff)
  };
  const diffProduction = diff.production || (
    diff.base && typeof diff.base === "object" ? diff.base as PublicModelItem : production
  );
  const healthStatus = health.health_status || health.status || production.health || "unknown";
  const healthAlerts = health.alerts || health.warnings || [];
  const selectedStatus = safeText(selected?.status, "");
  const canApprove = ["draft", "simulation", "pending"].includes(selectedStatus);
  const canActivate = selectedStatus === "approved";
  const canReject = ["draft", "simulation", "pending", "approved"].includes(selectedStatus);
  const canRollback = selectedStatus === "deprecated";

  return (
    <div className="space-y-5">
      <PageTitle
        title="模型管理"
        subtitle="登记 Production、Candidate、人工审批、回滚与上线后表现；模型版本变更必须由人确认。"
        tags={["Model Registry", "人工批准", "不会自动应用模型"]}
      />

      <section className="rounded-2xl border border-amber-400/40 bg-amber-400/5 p-5">
        <h2 className="text-lg font-black text-amber-100">所有修改需要人工确认</h2>
        <p className="mt-2 text-sm leading-6 text-slate-300">
          Candidate 从 simulation 不能自动跳转到 production。批准、拒绝、人工启用与回滚均要求后台登录、明确原因、二次确认和审计记录；本页面不会自动应用模型。
        </p>
      </section>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-slate-500">公开 Registry 使用 30 秒短缓存；刷新不会触发 Outcome 重算或外部行情请求。</p>
        <button className="btn" disabled={loading} onClick={() => void loadPublic(true)}>{loading ? "加载中" : "刷新公开状态"}</button>
      </div>

      {warnings.length ? <p className="rounded-xl border border-amber-400/30 p-3 text-sm text-amber-100">部分公开模型维度暂时不可用，其余结果仍可查看。</p> : null}

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-6">
        <MetricCard label="当前 Production" value={`${modelKey(production)} ${modelVersion(production)}`} tone="good" />
        <MetricCard label="模型状态" value={statusLabel(production.status)} />
        <MetricCard label="上线时间" value={formatDateTime(production.production_since || production.released_at || production.created_at)} />
        <MetricCard label="健康状态" value={healthLabel(health.health_label || healthStatus)} tone={healthTone(healthStatus)} />
        <MetricCard label="候选模型" value={candidates.length || "登录后查看"} tone="info" />
        <MetricCard label="自动动作" value={health.auto_action === true ? "安全边界异常" : "无"} tone={health.auto_action === true ? "bad" : "good"} />
      </section>

      <section className="panel p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-black text-white">当前生产模型</h2>
            <p className="mt-1 text-sm text-slate-400">公开区域只显示版本、状态和性能摘要，不展示完整参数或内部配置。</p>
          </div>
          <span className="chip">{modelKey(production)} · {modelVersion(production)}</span>
        </div>
        <div className="mt-4 grid gap-3 text-sm text-slate-300 md:grid-cols-2">
          <span>模型类型：{safeText(production.model_type)}</span>
          <span>来源版本：{safeText(production.source_version)}</span>
          <span>更新时间：{formatDateTime(production.updated_at)}</span>
          <ModelPerformanceSummary value={production.performance_summary} />
        </div>
      </section>

      <section className="panel p-5">
        <div className="mb-4">
          <h2 className="text-lg font-black text-white">Model Performance Timeline</h2>
          <p className="mt-1 text-sm text-slate-400">7d / 30d / 90d / all 快照只做监控和报警，不自动替换模型。</p>
        </div>
        {snapshots.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-sm text-slate-300">
              <thead className="border-b border-white/10 text-slate-500"><tr><th className="px-2 py-2">周期</th><th className="px-2 py-2">样本</th><th className="px-2 py-2">成功率</th><th className="px-2 py-2">平均收益</th><th className="px-2 py-2">平均回撤</th><th className="px-2 py-2">风险分</th><th className="px-2 py-2">快照时间</th></tr></thead>
              <tbody>{snapshots.map((item, index) => (
                <tr className="border-b border-white/5" key={`${safeText(item.period, "period")}-${index}`}>
                  <td className="px-2 py-3 font-bold text-white">{safeText(item.period)}</td>
                  <td className="px-2 py-3">{compact(item.sample_count)}</td>
                  <td className="px-2 py-3">{ratioText(item.success_ratio)}</td>
                  <td className="px-2 py-3">{pct(item.avg_return ?? item.avg_return_pct)}</td>
                  <td className="px-2 py-3">{pct(item.avg_drawdown ?? item.avg_drawdown_pct)}</td>
                  <td className="px-2 py-3">{valueText(item.risk_score)}</td>
                  <td className="px-2 py-3">{formatDateTime(item.created_at)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : <EmptyState title="暂无表现快照" text="Registry 初始化后会基于现有缓存聚合生成，不会重新请求历史行情。" />}
      </section>

      <section className={`panel border p-5 ${healthStatus === "degraded" ? "border-red-400/40" : healthStatus === "warning" ? "border-amber-400/40" : "border-emerald-400/30"}`}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div><h2 className="text-lg font-black text-white">Model Health Monitor</h2><p className="mt-1 text-sm text-slate-400">健康状态只报警，不自动切换、替换或回滚 Production。</p></div>
          <span className="chip">{healthLabel(health.health_label || healthStatus)}</span>
        </div>
        {healthAlerts.length ? <ul className="mt-4 list-disc space-y-2 pl-5 text-sm text-amber-100">{healthAlerts.map((item, index) => <li key={`${alertText(item)}-${index}`}>{alertText(item)}</li>)}</ul> : <p className="mt-4 text-sm text-emerald-200">当前没有模型健康告警。</p>}
        <p className="mt-3 text-xs text-slate-500">检查时间：{formatDateTime(health.checked_at || health.updated_at)} · auto_action=false</p>
      </section>

      <section className="panel p-5">
        <div className="mb-4"><h2 className="text-lg font-black text-white">历史模型</h2><p className="mt-1 text-sm text-slate-400">历史记录不可删除；选择旧版本只会进入人工回滚确认，不会直接生效。</p></div>
        {histories.length ? <div className="space-y-3">{histories.map((item, index) => (
          <div className="flex flex-col justify-between gap-3 rounded-xl border border-white/10 p-4 md:flex-row md:items-center" key={`${selectedKey(item)}-${index}`}>
            <div><h3 className="font-bold text-white">{modelKey(item)} {modelVersion(item)}</h3><p className="mt-1 text-sm text-slate-400">{statusLabel(item.status)} · {formatDateTime(item.updated_at || item.created_at)}</p><p className="mt-2 text-xs"><ModelPerformanceSummary value={item.performance_summary} /></p></div>
            <button className="btn" disabled={adminLoading} onClick={() => void selectForReview(item)}>选择并查看受保护 Diff</button>
          </div>
        ))}</div> : <EmptyState title="暂无历史版本" text="当前 Production 注册后会保留不可删除的版本历史。" />}
      </section>

      <section className="panel p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div><h2 className="text-lg font-black text-white">候选模型与 Approval 状态</h2><p className="mt-1 text-sm text-slate-400">候选参数差异属于受保护后台信息；登录后按需加载，绝不进入公共缓存。</p></div>
          <button className="btn" disabled={adminLoading} onClick={() => void loadPrivateModels()}>{adminLoading ? "处理中" : "登录后加载候选"}</button>
        </div>
        <p className="mt-3 rounded-xl border border-white/10 p-3 text-sm text-slate-300">{adminStatus}</p>
        {candidates.length ? <div className="mt-4 grid gap-3 md:grid-cols-2">{candidates.map((item, index) => (
          <button className="rounded-xl border border-white/10 p-4 text-left hover:border-cyan-400/40" key={`${selectedKey(item)}-${index}`} onClick={() => void selectForReview(item)}>
            <span className="font-bold text-white">{modelKey(item)} {modelVersion(item)}</span>
            <span className="mt-2 block text-sm text-slate-400">状态：{statusLabel(item.status)} · Approval：{statusLabel(item.approval_status || item.status)}</span>
            <span className="mt-2 block text-xs text-cyan-200">模拟：{simulationStatus(item)} · 点击加载 Diff</span>
          </button>
        ))}</div> : null}
      </section>

      <section className="panel p-5">
        <div className="mb-4"><h2 className="text-lg font-black text-white">Production / Candidate Diff</h2><p className="mt-1 text-sm text-slate-400">仅展示结构化变化项和模拟摘要，不渲染完整参数对象或内部配置。</p></div>
        {selected ? <>
          <div className="mb-4 flex flex-wrap gap-2"><span className="chip">Production：{modelVersion(diffProduction)}</span><span className="chip">所选：{modelKey(selected)} {modelVersion(selected)}</span><span className="chip">Approval：{statusLabel(diff.approval_status || selected.approval_status || selected.status)}</span></div>
          {selectedChanges.length ? <div className="overflow-x-auto"><table className="w-full min-w-[720px] text-left text-sm text-slate-300"><thead className="border-b border-white/10 text-slate-500"><tr><th className="px-2 py-2">修改参数</th><th className="px-2 py-2">旧值</th><th className="px-2 py-2">新值</th><th className="px-2 py-2">影响范围</th><th className="px-2 py-2">历史模拟结果</th></tr></thead><tbody>{selectedChanges.map((item, index) => <tr className="border-b border-white/5" key={`${safeText(item.parameter || item.label, "change")}-${index}`}><td className="px-2 py-3 font-bold text-white">{safeText(item.label || item.parameter)}</td><td className="px-2 py-3">{valueText(item.old ?? item.old_value)}</td><td className="px-2 py-3 text-cyan-200">{valueText(item.new ?? item.new_value)}</td><td className="px-2 py-3">{safeText(item.impact_scope, "待人工评估")}</td><td className="px-2 py-3">{valueText(item.simulation_result)}</td></tr>)}</tbody></table></div> : <EmptyState title="暂无结构化 Diff" text="需后台登录，或该版本没有参数变化。不会从公共接口推断完整模型参数。" />}
          {Object.keys(selectedSimulation).length ? <div className="mt-4 grid gap-3 text-sm text-slate-300 md:grid-cols-3"><span>模拟样本：{compact(selectedSimulation.sample_count ?? selectedSimulation.mature_sample_count)}</span><span>成功率变化：{ratioText(selectedSimulation.success_ratio_delta)}</span><span>平均收益变化：{pct(selectedSimulation.avg_return_delta ?? selectedSimulation.avg_return_delta_pct)}</span></div> : null}
        </> : <EmptyState title="请选择模型版本" text="选择候选或历史版本后，可在已登录后台会话中读取受保护 Diff 摘要。" />}
      </section>

      <section className="rounded-2xl border border-amber-400/40 bg-slate-950/50 p-5">
        <h2 className="text-lg font-black text-white">人工审批、启用与 Rollback</h2>
        <p className="mt-2 text-sm text-amber-100">所有修改需要人工确认。simulation → production 禁止自动跳转；每次操作均以 Jobs 后台任务执行并写入审计记录。</p>
        <label className="mt-4 block text-sm font-bold text-slate-300" htmlFor="model-approval-reason">人工原因（必填）</label>
        <textarea id="model-approval-reason" className="mt-2 min-h-24 w-full rounded-xl border border-white/10 bg-slate-950 p-3 text-sm text-white" onChange={(event) => setReason(event.target.value)} placeholder="说明批准、拒绝、启用或回滚原因" value={reason} />
        <div className="mt-4 flex flex-wrap gap-2">
          <button className="btn" disabled={!canApprove || adminLoading} onClick={() => void submitAction("approve")}>批准（不启用）</button>
          <button className="btn" disabled={!canActivate || adminLoading} onClick={() => void submitAction("activate")}>人工启用 Production</button>
          <button className="btn" disabled={!canReject || adminLoading} onClick={() => void submitAction("reject")}>拒绝</button>
          <button className="btn" disabled={!canRollback || adminLoading} onClick={() => void submitAction("rollback")}>回滚</button>
        </div>
        {jobMessage ? <p className="mt-4 rounded-xl border border-white/10 p-3 text-sm text-slate-200">{jobMessage}</p> : null}
        <p className="mt-3 text-xs text-slate-500">当前选择：{selected ? `${modelKey(selected)} ${modelVersion(selected)} · ${statusLabel(selectedStatus)}` : "无"}。先批准才可人工启用；只有已停用历史版本可作为回滚目标。页面不会发送 token、Cookie 内容、完整参数或内部配置。</p>
      </section>
    </div>
  );
}
