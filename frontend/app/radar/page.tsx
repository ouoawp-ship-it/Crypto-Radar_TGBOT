"use client";

import { useEffect, useState } from "react";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { PageTitle } from "@/components/PageTitle";
import { SignalCard } from "@/components/SignalCard";
import { getSignals, getSignalStats, getTimeline } from "@/lib/api";
import type { SignalItem } from "@/lib/types";

export default function RadarPage() {
  const [filters, setFilters] = useState({ symbol: "", module: "", status: "", q: "", window_sec: "604800" });
  const [signals, setSignals] = useState<SignalItem[]>([]);
  const [stats, setStats] = useState<Record<string, unknown>>({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    setError("");
    try {
      const [list, statPayload] = await Promise.all([
        getSignals({ ...filters, limit: 40 }),
        getSignalStats(Number(filters.window_sec || 86400)),
        getTimeline({ ...filters, limit: 100 })
      ]);
      setSignals(list.items || []);
      setStats(statPayload);
    } catch (err) {
      setError(err instanceof Error ? err.message : "信号雷达加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) return <ErrorState message={error} onRetry={load} />;

  return (
    <div className="space-y-5">
      <PageTitle title="信号雷达" subtitle="查看最新公开信号流，按币种、模块、状态、关键词和时间窗口筛选。" tags={["信号流", "模块筛选", "详情入口"]} />
      <section className="panel grid gap-3 p-4 md:grid-cols-6">
        <input className="input" placeholder="币种" value={filters.symbol} onChange={(event) => setFilters({ ...filters, symbol: event.target.value })} />
        <select className="input" value={filters.module} onChange={(event) => setFilters({ ...filters, module: event.target.value })}>
          <option value="">全部模块</option>
          <option value="launch">启动雷达</option>
          <option value="funding">资金费率</option>
          <option value="flow">资金流</option>
          <option value="structure">结构雷达</option>
          <option value="structure_review">结构复盘</option>
        </select>
        <select className="input" value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}>
          <option value="">全部状态</option>
          <option value="sent">已发送</option>
          <option value="blocked">已阻止</option>
          <option value="failed">失败</option>
          <option value="skipped">已跳过</option>
        </select>
        <select className="input" value={filters.window_sec} onChange={(event) => setFilters({ ...filters, window_sec: event.target.value })}>
          <option value="86400">24 小时</option>
          <option value="604800">7 天</option>
          <option value="2592000">30 天</option>
        </select>
        <input className="input" placeholder="关键词" value={filters.q} onChange={(event) => setFilters({ ...filters, q: event.target.value })} />
        <button className="btn" onClick={load}>
          搜索
        </button>
      </section>
      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard label="信号总数" value={stats.total || 0} hint={loading ? "加载中" : "当前窗口"} />
        <MetricCard label="已发送" value={stats.sent || 0} tone="good" />
        <MetricCard label="阻止/失败" value={Number(stats.blocked || 0) + Number(stats.failed || 0)} tone="bad" />
        <MetricCard label="当前显示" value={signals.length} />
      </section>
      <section className="grid gap-4 md:grid-cols-2">
        {signals.map((item) => (
          <SignalCard key={item.id || `${item.symbol}-${item.time}`} item={item} />
        ))}
      </section>
    </div>
  );
}
