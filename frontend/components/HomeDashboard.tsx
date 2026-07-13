"use client";

import Link from "next/link";
import { useState } from "react";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { MetricCard } from "./MetricCard";
import { PageTitle } from "./PageTitle";
import { SignalCard } from "./SignalCard";
import {
  getSignalStats,
  getSignals,
  invalidatePublicApiCache,
  type HomeDashboardData
} from "@/lib/api";
import { compact } from "@/lib/format";

function readNumber(record: Record<string, unknown> | undefined, ...keys: string[]) {
  for (const key of keys) {
    const value = record?.[key];
    if (typeof value === "number") return value;
    if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  }
  return undefined;
}

export function HomeDashboard({ initialData = {} }: { initialData?: HomeDashboardData }) {
  const [data, setData] = useState<HomeDashboardData>(initialData);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    invalidatePublicApiCache();
    setLoading(true);
    setError("");
    try {
      const [signalStats, signals] = await Promise.all([
        getSignalStats(86400),
        getSignals({ limit: 8, window_sec: 86400 })
      ]);
      setData({ signalStats, signals: signals.items || [] });
    } catch (err) {
      setError(err instanceof Error ? err.message : "公开信号暂时不可用，请稍后重试。");
    } finally {
      setLoading(false);
    }
  }

  if (error && !data.signalStats && !data.signals?.length) {
    return <ErrorState message={error} onRetry={load} />;
  }

  const total = readNumber(data.signalStats, "total", "count", "signals_count");
  const sent = readNumber(data.signalStats, "sent", "sent_count");
  const blocked = readNumber(data.signalStats, "blocked", "blocked_count");
  const failed = readNumber(data.signalStats, "failed", "failed_count");

  return (
    <div className="space-y-5">
      <PageTitle
        title="总览"
        subtitle="集中查看雷达产生的最新信号与推送状态，快速确认系统是否正常运行。"
        tags={["实时信号", "推送状态", "只读公开数据"]}
      />

      {loading ? <div className="panel p-4 text-sm text-text-secondary">正在刷新公开信号...</div> : null}
      {data.error ? (
        <div className="panel border-warn/25 bg-warn/5 p-4 text-sm text-amber-700">{data.error}</div>
      ) : null}

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="24 小时信号" value={compact(total)} hint="最近 24 小时" tone="info" />
        <MetricCard label="已发送" value={compact(sent)} hint="成功进入推送流程" tone="good" />
        <MetricCard label="已阻止" value={compact(blocked)} hint="被安全规则拦截" tone="warn" />
        <MetricCard label="发送失败" value={compact(failed)} hint="建议前往后台检查" tone="bad" />
      </section>

      <section className="panel p-4 sm:p-5">
        <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="section-title">最新信号</h2>
            <p className="mt-1 text-sm text-text-muted">按时间倒序展示雷达产生的公开信号。</p>
          </div>
          <div className="flex gap-2">
            <button className="btn-secondary" onClick={load} disabled={loading}>
              {loading ? "刷新中" : "刷新"}
            </button>
            <Link className="btn" href="/radar">打开信号雷达</Link>
          </div>
        </div>
        <div className="grid gap-3 xl:grid-cols-2">
          {(data.signals || []).map((item) => (
            <SignalCard key={item.id || `${item.symbol}-${item.time}`} item={item} />
          ))}
        </div>
        {!loading && !(data.signals || []).length ? (
          <EmptyState title="暂无公开信号" text="雷达产生新信号后会自动显示在这里。" />
        ) : null}
      </section>
    </div>
  );
}
