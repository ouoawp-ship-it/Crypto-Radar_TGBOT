"use client";

import { useEffect, useState } from "react";
import { BacktestMatrix } from "@/components/BacktestMatrix";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { OutcomeCard } from "@/components/OutcomeCard";
import { PageTitle } from "@/components/PageTitle";
import { getBacktestDecision, getBacktestDetail, getBacktestMatrix, invalidatePublicApiCache } from "@/lib/api";
import { pct, ratioPct, safeText } from "@/lib/format";
import type { BacktestMatrixPayload, BacktestPayload, OutcomeItem } from "@/lib/types";

export default function BacktestPage() {
  const [horizon, setHorizon] = useState("1h");
  const [summary, setSummary] = useState<BacktestPayload>({});
  const [matrix, setMatrix] = useState<BacktestMatrixPayload>({});
  const [detail, setDetail] = useState<OutcomeItem[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function load(nextHorizon = horizon, refresh = false) {
    if (refresh) invalidatePublicApiCache();
    setLoading(true);
    setError("");
    try {
      const [payload, matrixPayload, detailPayload] = await Promise.all([
        getBacktestDecision({ horizon: nextHorizon, window_sec: 2592000 }),
        getBacktestMatrix({ window_sec: 2592000 }),
        getBacktestDetail({ decision: "risk_alert", horizon: nextHorizon, limit: 8, window_sec: 2592000 })
      ]);
      setSummary(payload);
      setMatrix(matrixPayload);
      setDetail(detailPayload.items || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "决策回测加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load("1h");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) return <ErrorState message={error} onRetry={() => load(horizon, true)} />;

  const s = summary.summary || {};
  const d = summary.model_diagnosis || {};

  return (
    <div className="space-y-5">
      <PageTitle
        title="决策回测"
        subtitle="基于 signal_outcomes 统计不同决策在后续窗口里的表现，用于模型诊断和校准。"
        tags={["模型诊断", "样本质量", "不执行自动交易"]}
      />
      <div className="panel flex flex-wrap gap-2 p-4">
        {["1h", "4h", "24h", "72h", "all"].map((item) => (
          <button
            className={`btn ${horizon === item ? "bg-cyanline/25" : ""}`}
            key={item}
            onClick={() => {
              setHorizon(item);
              void load(item, true);
            }}
          >
            {item === "all" ? "全部周期" : item}
          </button>
        ))}
      </div>
      <section className="grid gap-4 md:grid-cols-3 xl:grid-cols-6">
        <MetricCard label="样本总数" value={s.total_count ?? "-"} />
        <MetricCard label="已计算样本" value={s.success_count ?? "-"} tone="good" />
        <MetricCard label="覆盖率" value={ratioPct(s.coverage_ratio)} />
        <MetricCard label="平均最终涨跌" value={pct(s.avg_final_return_pct)} />
        <MetricCard label="正收益比例" value={ratioPct(s.positive_ratio)} tone="good" />
        <MetricCard label="明显回撤比例" value={ratioPct(s.drawdown_ratio)} tone="warn" />
      </section>
      <section className="grid gap-5 xl:grid-cols-[0.9fr_1.1fr]">
        <div className="panel p-5">
          <h2 className="text-lg font-black text-white">模型诊断</h2>
          <p className="mt-2 text-sm leading-6 text-slate-300">{safeText(d.overall_summary, "样本仍在积累。")}</p>
          <div className="mt-4 grid gap-3 text-sm text-slate-400">
            <div>
              <b className="text-green-200">有效项：</b>
              {safeText((d.strengths || []).join("；"), "暂无")}
            </div>
            <div>
              <b className="text-amber-200">弱项：</b>
              {safeText((d.weaknesses || []).join("；"), "暂无")}
            </div>
            <div>
              <b className="text-cyan-200">校准建议：</b>
              {safeText((d.calibration_hints || []).join("；"), "继续观察")}
            </div>
            <div>
              <b className="text-slate-200">样本质量：</b>
              {safeText(s.sample_quality, "样本不足")}
            </div>
          </div>
        </div>
        <BacktestMatrix data={matrix} />
      </section>
      <section>
        <h2 className="mb-3 text-lg font-black text-white">风险警报样本详情</h2>
        <div className="grid gap-4 md:grid-cols-2">
          {detail.map((item, index) => (
            <OutcomeCard key={`${item.symbol}-${index}`} item={item} />
          ))}
        </div>
        {!loading && !detail.length ? <EmptyState title="暂无样本详情" text="等待更多已计算的风险警报样本。" /> : null}
      </section>
    </div>
  );
}
