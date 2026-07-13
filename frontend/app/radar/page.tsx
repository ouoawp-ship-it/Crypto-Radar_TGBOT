"use client";

import { useEffect, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { PageTitle } from "@/components/PageTitle";
import { SignalCard } from "@/components/SignalCard";
import { getSignals, getSignalStats, getTimeline, invalidatePublicApiCache } from "@/lib/api";
import { compact } from "@/lib/format";
import type { SignalItem } from "@/lib/types";

type RadarFilters = {
  symbol: string;
  module: string;
  status: string;
  q: string;
  window_sec: string;
};

const defaultFilters: RadarFilters = { symbol: "", module: "", status: "", q: "", window_sec: "604800" };

export default function RadarPage() {
  const [filters, setFilters] = useState<RadarFilters>(defaultFilters);
  const [signals, setSignals] = useState<SignalItem[]>([]);
  const [timelineCount, setTimelineCount] = useState<number | undefined>();
  const [stats, setStats] = useState<Record<string, unknown>>({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function load(nextFilters = filters, refresh = false) {
    if (refresh) invalidatePublicApiCache();
    setLoading(true);
    setError("");
    try {
      const [list, statPayload, timelinePayload] = await Promise.all([
        getSignals({ ...nextFilters, limit: 40 }),
        getSignalStats(Number(nextFilters.window_sec || 86400)),
        getTimeline({ ...nextFilters, limit: 100 })
      ]);
      setSignals(list.items || []);
      setStats(statPayload);
      setTimelineCount(timelinePayload.count || timelinePayload.items?.length || timelinePayload.groups?.reduce((total, group) => total + (group.items?.length || 0), 0));
    } catch (err) {
      setError(err instanceof Error ? err.message : "信号雷达加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load(defaultFilters);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) return <ErrorState message={error} onRetry={() => load(filters, true)} />;

  return (
    <div className="space-y-5">
      <PageTitle
        title="信号雷达"
        subtitle="筛选最新公开信号流，按币种、模块、状态和时间窗口定位可复盘事件。"
        tags={["信号流", "模块筛选", "单币入口"]}
      />

      <section className="panel p-4">
        <div className="grid gap-3 md:grid-cols-6">
          <input className="input" placeholder="币种，如 BTCUSDT" value={filters.symbol} onChange={(event) => setFilters({ ...filters, symbol: event.target.value.toUpperCase() })} />
          <select className="input" value={filters.module} onChange={(event) => setFilters({ ...filters, module: event.target.value })}>
            <option value="">全部模块</option>
            <option value="launch">启动雷达</option>
            <option value="funding">资金费率</option>
            <option value="flow">资金流</option>
            <option value="structure">结构雷达</option>
            <option value="structure_review">结构复盘</option>
            <option value="announcement">公告</option>
          </select>
          <select className="input" value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}>
            <option value="">全部状态</option>
            <option value="sent">已发送</option>
            <option value="blocked">已阻止</option>
            <option value="failed">失败</option>
            <option value="skipped">已跳过</option>
            <option value="dry_run">演练</option>
          </select>
          <select className="input" value={filters.window_sec} onChange={(event) => setFilters({ ...filters, window_sec: event.target.value })}>
            <option value="86400">24 小时</option>
            <option value="604800">7 天</option>
            <option value="2592000">30 天</option>
          </select>
          <input className="input" placeholder="关键词" value={filters.q} onChange={(event) => setFilters({ ...filters, q: event.target.value })} />
          <div className="flex gap-2">
            <button className="btn flex-1" onClick={() => load(filters, true)}>
              {loading ? "搜索中" : "搜索"}
            </button>
            <button
              className="btn-secondary"
              onClick={() => {
                setFilters(defaultFilters);
                void load(defaultFilters, true);
              }}
            >
              重置
            </button>
          </div>
        </div>
      </section>

      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard label="信号总数" value={compact(stats.total ?? stats.count)} hint={loading ? "正在加载数据..." : "当前窗口"} tone="info" />
        <MetricCard label="已发送" value={compact(stats.sent)} tone="good" />
        <MetricCard label="阻止/失败" value={compact(Number(stats.blocked || 0) + Number(stats.failed || 0))} tone="bad" />
        <MetricCard label="时间线事件" value={compact(timelineCount)} />
      </section>

      <section className="grid gap-4 md:grid-cols-2">
        {signals.map((item) => (
          <SignalCard key={item.id || `${item.symbol}-${item.time}`} item={item} />
        ))}
      </section>
      {!loading && !signals.length ? <EmptyState title="暂无符合条件的信号" text="可以放宽币种、模块、状态或时间窗口筛选。" /> : null}
    </div>
  );
}
