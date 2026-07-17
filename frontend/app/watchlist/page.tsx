"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { PageTitle } from "@/components/PageTitle";
import { getWatchlistMarket } from "@/lib/api";
import { formatMetricValue, freshnessLabel, safeText } from "@/lib/format";
import type { WatchlistMarketItem } from "@/lib/types";
import { loadWatchlist, normalizeWatchSymbol, saveWatchlist, WATCHLIST_LIMIT } from "@/lib/watchlist";

export default function WatchlistPage() {
  const [symbols, setSymbols] = useState<string[]>([]);
  const [items, setItems] = useState<WatchlistMarketItem[]>([]);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [inputError, setInputError] = useState("");

  async function load(nextSymbols: string[], refresh = false) {
    if (!nextSymbols.length) {
      setItems([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const payload = await getWatchlistMarket(nextSymbols, { bypassCache: refresh });
      setItems(payload.items || []);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "自选行情加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const initial = loadWatchlist();
    setSymbols(initial);
    void load(initial);
  }, []);

  function add(event: FormEvent) {
    event.preventDefault();
    const symbol = normalizeWatchSymbol(draft);
    if (!symbol) {
      setInputError("请输入有效币种，例如 BTC 或 BTCUSDT");
      return;
    }
    if (!symbols.includes(symbol) && symbols.length >= WATCHLIST_LIMIT) {
      setInputError(`最多保存 ${WATCHLIST_LIMIT} 个币种`);
      return;
    }
    const next = saveWatchlist([...symbols, symbol]);
    setSymbols(next);
    setDraft("");
    setInputError("");
    void load(next, true);
  }

  function remove(symbol: string) {
    const next = saveWatchlist(symbols.filter((item) => item !== symbol));
    setSymbols(next);
    setItems((current) => current.filter((item) => item.symbol !== symbol));
  }

  return (
    <div className="space-y-5">
      <PageTitle title="我的自选" subtitle="把重要币种保存在当前浏览器，用服务端聚合快照快速复查；不需要账号，也不会上传个人交易信息。" tags={["本地保存", `最多 ${WATCHLIST_LIMIT} 个`, "只读行情"]} />

      <form className="panel flex flex-col gap-3 p-4 sm:flex-row sm:items-end" onSubmit={add}>
        <label className="min-w-0 flex-1"><span className="mb-2 block text-xs font-semibold text-text-secondary">添加币种</span><input className="input w-full" placeholder="BTC 或 BTCUSDT" value={draft} onChange={(event) => setDraft(event.target.value.toUpperCase())} /></label>
        <button className="btn h-10 sm:w-28" type="submit">加入自选</button>
        <button className="btn-secondary h-10 sm:w-24" disabled={!symbols.length || loading} onClick={() => void load(symbols, true)} type="button">{loading ? "刷新中" : "刷新"}</button>
      </form>
      {inputError ? <p className="px-1 text-xs text-red-700">{inputError}</p> : null}
      {error ? <ErrorState message={error} onRetry={() => void load(symbols, true)} /> : null}

      {loading && !items.length ? <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{Array.from({ length: Math.max(2, symbols.length) }).map((_, index) => <div className="h-56 animate-pulse rounded-xl bg-surface-container" key={index} />)}</div> : null}

      {!loading && !symbols.length ? <section className="panel p-4"><EmptyState title="还没有关注的资产" text="在雷达、资金榜单或单币详情中点击星标，即可在这里持续跟踪。" /><div className="mt-3 flex justify-center gap-2"><Link className="btn" href="/radar">去信号雷达</Link><Link className="btn-secondary" href="/funds">查看资金榜单</Link></div></section> : null}

      {items.length ? (
        <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {items.map((item) => {
            const market = item.market;
            const metrics = market?.metrics || {};
            const flow = item.flow;
            return (
              <article className="panel overflow-hidden" key={item.symbol}>
                <div className="flex items-start justify-between gap-3 border-b border-border-subtle p-4">
                  <div><div className="table-number text-xl font-semibold text-text-primary">{safeText(item.symbol)}</div><div className="mt-1 text-xs text-text-muted">{item.ok ? freshnessLabel(market?.status, market?.age_sec) : safeText(item.error, "暂时不可用")}</div></div>
                  <button aria-label={`移除 ${item.symbol}`} className="grid h-9 w-9 place-items-center rounded-lg border border-border-subtle text-text-muted hover:bg-surface-canvas" onClick={() => remove(String(item.symbol || ""))}>×</button>
                </div>
                <div className="grid grid-cols-2 gap-px bg-border-subtle">
                  {[["价格", metrics.price], ["24h", metrics.price_24h_pct], ["成交额", metrics.quote_volume], ["OI", metrics.oi_value]].map(([label, metric]) => {
                    const value = metric as typeof metrics.price;
                    return <div className="bg-white p-4" key={String(label)}><div className="text-[11px] font-semibold text-text-muted">{String(label)}</div><div className="table-number mt-1 text-base font-semibold text-text-primary">{value?.value == null ? "—" : formatMetricValue(value.value, value.unit)}</div></div>;
                  })}
                </div>
                <div className="grid grid-cols-2 gap-px border-t border-border-subtle bg-border-subtle">
                  {[["现货 CVD", flow?.spot_net_flow_usd, "usd"], ["合约 CVD", flow?.futures_net_flow_usd, "usd"], ["1h OI", flow?.oi_change_pct, "percent"], ["资金费率", flow?.funding_pct, "percent_per_cycle"]].map(([label, value, unit]) => <div className="bg-surface-panel p-3" key={String(label)}><div className="text-[10px] font-semibold text-text-muted">{String(label)}</div><div className="table-number mt-1 text-sm font-semibold text-text-primary">{formatMetricValue(value, String(unit))}</div></div>)}
                </div>
                <div className="grid grid-cols-2 gap-2 p-4"><Link className="btn-secondary" href={item.coin_url || `/coin/${item.symbol}`}>查看上下文</Link><Link className="btn" href={`/radar?symbol=${item.symbol}`}>查看信号</Link></div>
              </article>
            );
          })}
        </section>
      ) : null}
    </div>
  );
}
