"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { MetricCard } from "@/components/MetricCard";
import { PageTitle } from "@/components/PageTitle";
import { getLifecycleSummary, getLifecycles, invalidatePublicApiCache } from "@/lib/api";
import { compact, pct, safeText } from "@/lib/format";
import type { LifecycleItem, LifecycleSummaryPayload } from "@/lib/types";

export default function LifecyclePage() {
  const [summary, setSummary] = useState<LifecycleSummaryPayload>({});
  const [items, setItems] = useState<LifecycleItem[]>([]);
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
      const [summaryPayload, listPayload] = await Promise.all([
        getLifecycleSummary(),
        getLifecycles({ symbol, state, level, risk, limit: 80 })
      ]);
      setSummary(summaryPayload);
      setItems(listPayload.items || []);
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

  return (
    <div className="space-y-5">
      <PageTitle
        title="生命周期跟随"
        subtitle="一个币首次出现有效信号后自动建档，持续跟随 Binance 价格、成交量、OI、CVD 和资金费率变化。"
        tags={["Binance 核心口径", "单币生命周期", "不执行自动交易"]}
      />
      <section className="grid gap-4 md:grid-cols-3 xl:grid-cols-6">
        <MetricCard label="活跃生命周期" value={compact(s.active_count)} tone="info" />
        <MetricCard label="1h 升级数" value={compact(s.upgraded_1h_count)} tone="good" />
        <MetricCard label="4h 升级数" value={compact(s.upgraded_4h_count)} tone="good" />
        <MetricCard label="大周期确认" value={compact(s.trend_confirmed_count)} tone="good" />
        <MetricCard label="风险升高" value={compact(s.risk_warning_count)} tone="bad" />
        <MetricCard label="短线冷却" value={compact(s.cooling_count)} tone="warn" />
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
