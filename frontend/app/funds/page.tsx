"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CandlestickChart } from "@/components/CandlestickChart";
import { MetricSeriesChart } from "@/components/MetricSeriesChart";
import { getCoinContext, getFundsAssets, getFundsSectors, getWorkstationFundsOpenInterest } from "@/lib/api";
import type { CoinContext, CoinSeriesPoint, CrossExchangeOpenInterest, FundsAsset, FundsAssetsPayload, FundsSectorsPayload } from "@/lib/types";

const SPANS = [
  { key: "16h", interval: "15m", bars: 64 },
  { key: "2d", interval: "1h", bars: 48 },
  { key: "4d", interval: "1h", bars: 96 },
  { key: "5d", interval: "1h", bars: 120 },
  { key: "15d", interval: "4h", bars: 90 },
  { key: "60d", interval: "1d", bars: 60 }
] as const;

type MarketType = "spot" | "futures";
type SpanKey = (typeof SPANS)[number]["key"];

function number(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function money(value: unknown, signed = true) {
  const parsed = number(value);
  if (parsed === null) return "—";
  const sign = signed ? (parsed > 0 ? "+" : parsed < 0 ? "−" : "") : "";
  const absolute = Math.abs(parsed);
  if (absolute >= 1e12) return `${sign}$${(absolute / 1e12).toFixed(2)}T`;
  if (absolute >= 1e9) return `${sign}$${(absolute / 1e9).toFixed(2)}B`;
  if (absolute >= 1e6) return `${sign}$${(absolute / 1e6).toFixed(1)}M`;
  if (absolute >= 1e3) return `${sign}$${(absolute / 1e3).toFixed(1)}K`;
  return `${sign}$${absolute.toFixed(2)}`;
}

function percent(value: unknown, digits = 2) {
  const parsed = number(value);
  return parsed === null ? "—" : `${parsed > 0 ? "+" : ""}${parsed.toFixed(digits)}%`;
}

function tone(value: unknown) {
  const parsed = number(value);
  return parsed === null || parsed === 0 ? "text-text-secondary" : parsed > 0 ? "text-good" : "text-risk";
}

function PanelHeader({ title, detail, action }: { title: string; detail?: string; action?: React.ReactNode }) {
  return <div className="workstation-panel-header"><div className="min-w-0"><h2 className="truncate text-[12px] font-semibold text-text-primary">{title}</h2>{detail ? <p className="truncate text-[9px] text-text-muted">{detail}</p> : null}</div>{action}</div>;
}

function FlowPriceChart({ points, marketType }: { points: CoinSeriesPoint[]; marketType: MarketType }) {
  const metric = marketType === "spot" ? "spot_flow_usd" : "futures_flow_usd";
  let running = 0;
  const source = points.map((point, index) => {
    const flow = number(point[metric]) || 0;
    running += flow;
    return { index, cumulative: running, price: number(point.price) };
  }).filter((point) => point.price !== null);
  if (source.length < 2) return <div className="grid h-[150px] place-items-center text-[10px] text-text-muted">累计资金样本不足</div>;
  const flowMin = Math.min(...source.map((item) => item.cumulative));
  const flowMax = Math.max(...source.map((item) => item.cumulative));
  const priceMin = Math.min(...source.map((item) => Number(item.price)));
  const priceMax = Math.max(...source.map((item) => Number(item.price)));
  const x = (index: number) => 12 + index / Math.max(1, source.length - 1) * 576;
  const y = (value: number, min: number, max: number) => 132 - (value - min) / Math.max(max - min, 1e-9) * 108;
  const path = (key: "cumulative" | "price", min: number, max: number) => source.map((item, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(Number(item[key]), min, max).toFixed(1)}`).join(" ");
  return (
    <div className="px-3 pb-2 pt-2">
      <div className="flex items-center gap-4 text-[9px]"><span className="text-primary-700">● 累计资金 {money(source.at(-1)?.cumulative)}</span><span className="text-text-muted">● 价格 {money(source.at(-1)?.price, false)}</span><span className="ml-auto text-text-muted">双轴归一化，仅比较趋势</span></div>
      <svg aria-label="累计资金与价格时序" className="mt-1 h-[150px] w-full" role="img" viewBox="0 0 600 150">
        {[24, 78, 132].map((value) => <line className="stroke-border-subtle" strokeDasharray="3 5" x1="12" x2="588" y1={value} y2={value} key={value} />)}
        <path className="fill-none stroke-primary-500" d={path("cumulative", flowMin, flowMax)} strokeWidth="2" />
        <path className="fill-none stroke-text-muted" d={path("price", priceMin, priceMax)} strokeWidth="1.5" />
      </svg>
    </div>
  );
}

function AssetList({ assets, selected, onSelect }: { assets: FundsAsset[]; selected: string; onSelect: (symbol: string) => void }) {
  return <div className="workstation-scroll min-h-0 flex-1 overflow-y-auto">{assets.map((item, index) => <button className={`grid h-10 w-full grid-cols-[22px_minmax(0,1fr)_76px] items-center gap-2 border-b border-border-subtle px-2 text-left hover:bg-surface-container/55 ${item.symbol === selected ? "bg-primary-500/8" : ""}`} onClick={() => onSelect(item.symbol || "")} type="button" key={item.symbol}><span className="text-right font-mono text-[9px] text-text-muted">{index + 1}</span><span className="min-w-0"><span className="block truncate text-[10px] font-semibold text-text-primary">{item.coin || item.symbol}</span><span className="block truncate text-[8px] text-text-muted">{item.sector?.primary_sector_label || "其他"}</span></span><span className="text-right"><span className={`block font-mono text-[10px] font-semibold ${tone(item.net_flow_usd)}`}>{money(item.net_flow_usd)}</span><span className={`block font-mono text-[8px] ${tone(item.price_change_pct)}`}>{percent(item.price_change_pct)}</span></span></button>)}{!assets.length ? <div className="grid h-48 place-items-center text-[10px] text-text-muted">资产资金数据积累中</div> : null}</div>;
}

function CrossOi({ payload }: { payload: CrossExchangeOpenInterest }) {
  const rows = payload.exchanges || [];
  return <div className="p-2.5"><div className="mb-2 flex items-baseline justify-between"><span className="text-[9px] text-text-muted">可用场所合计</span><span className="font-mono text-[12px] font-semibold text-text-primary">{money(payload.total_oi_usd, false)}</span></div>{rows.map((item) => <div className="mb-2" key={item.exchange}><div className="flex items-center justify-between text-[9px]"><span className="capitalize text-text-secondary">{item.exchange}</span><span className="font-mono text-text-primary">{item.status === "ready" ? `${money(item.oi_usd, false)} · ${percent(item.share_pct)}` : "不可用"}</span></div><div className="mt-1 h-1 overflow-hidden rounded-sm bg-surface-container"><div className="h-full bg-primary-500" style={{ width: `${Math.max(0, Math.min(100, Number(item.share_pct || 0)))}%` }} /></div></div>)}<p className="mt-2 text-[8px] leading-4 text-text-muted">缺失场所不按 0 参与分母；OKX 优先采用 oiUsd，其余按标记价格归一。</p></div>;
}

export default function FundsPage() {
  const [marketType, setMarketType] = useState<MarketType>("futures");
  const [span, setSpan] = useState<SpanKey>("4d");
  const [selected, setSelected] = useState("BTCUSDT");
  const [query, setQuery] = useState("");
  const [spotSectors, setSpotSectors] = useState<FundsSectorsPayload>({});
  const [futuresSectors, setFuturesSectors] = useState<FundsSectorsPayload>({});
  const [spotAssets, setSpotAssets] = useState<FundsAssetsPayload>({});
  const [futuresAssets, setFuturesAssets] = useState<FundsAssetsPayload>({});
  const [coin, setCoin] = useState<CoinContext>({});
  const [crossOi, setCrossOi] = useState<CrossExchangeOpenInterest>({});
  const [loading, setLoading] = useState(true);
  const [coinLoading, setCoinLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const symbol = new URLSearchParams(window.location.search).get("symbol")?.toUpperCase();
    if (symbol && /^[A-Z0-9]{2,20}(?:USDT)?$/.test(symbol)) setSelected(symbol.endsWith("USDT") ? symbol : `${symbol}USDT`);
  }, []);

  const loadOverview = useCallback(async (bypassCache = false) => {
    setLoading(true);
    setError("");
    try {
      const options = { bypassCache };
      const [spotSectorData, futuresSectorData, spotAssetData, futuresAssetData] = await Promise.all([
        getFundsSectors(3600, "spot", options),
        getFundsSectors(3600, "futures", options),
        getFundsAssets({ window_sec: 3600, market_type: "spot", sort: "net_flow_usd", direction: "desc", page_size: 100 }, options),
        getFundsAssets({ window_sec: 3600, market_type: "futures", sort: "net_flow_usd", direction: "desc", page_size: 100 }, options)
      ]);
      setSpotSectors(spotSectorData);
      setFuturesSectors(futuresSectorData);
      setSpotAssets(spotAssetData);
      setFuturesAssets(futuresAssetData);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "资金总览加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadCoin = useCallback(async (bypassCache = false) => {
    if (!selected) return;
    setCoinLoading(true);
    const config = SPANS.find((item) => item.key === span) || SPANS[2];
    try {
      const [context, oi] = await Promise.all([
        getCoinContext(selected, { bypassCache }, { market_type: marketType, interval: config.interval, bars: config.bars }),
        getWorkstationFundsOpenInterest(selected, { bypassCache })
      ]);
      setCoin(context);
      setCrossOi(oi);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "单币资金视图加载失败");
    } finally {
      setCoinLoading(false);
    }
  }, [marketType, selected, span]);

  useEffect(() => { void loadOverview(); }, [loadOverview]);
  useEffect(() => { void loadCoin(); }, [loadCoin]);
  useEffect(() => { window.history.replaceState({}, "", `${window.location.pathname}?symbol=${selected}`); }, [selected]);

  const currentAssets = marketType === "spot" ? spotAssets.items || [] : futuresAssets.items || [];
  const visibleAssets = currentAssets.filter((item) => !query || String(item.symbol || "").includes(query));
  const selectedAsset = currentAssets.find((item) => item.symbol === selected) || futuresAssets.items?.find((item) => item.symbol === selected) || spotAssets.items?.find((item) => item.symbol === selected);
  const series = coin.series?.points || [];
  const oiValues = (futuresAssets.items || []).map((item) => Number(item.oi_usd)).filter((value) => Number.isFinite(value) && value > 0).sort((a, b) => b - a);
  const totalOi = oiValues.reduce((sum, value) => sum + value, 0);
  const topShare = (count: number) => totalOi ? oiValues.slice(0, count).reduce((sum, value) => sum + value, 0) / totalOi * 100 : null;
  const spotSummary = spotSectors.summary || {};
  const futuresSummary = futuresSectors.summary || {};

  return (
    <div aria-busy={loading || coinLoading} className="workstation-page flex min-h-0 flex-col gap-2 p-[10px]" data-testid="funds-workstation">
      <section className="workstation-panel flex h-10 shrink-0 items-center gap-2 overflow-x-auto px-2 workstation-scroll">
        <div aria-label="市场类型" className="flex rounded-sm border border-border-subtle bg-surface-low p-0.5" role="group">{(["futures", "spot"] as const).map((value) => <button aria-pressed={marketType === value} className={`h-6 min-w-14 rounded-[2px] px-2 text-[9px] font-semibold ${marketType === value ? "bg-primary-500 text-on-primary" : "text-text-muted"}`} onClick={() => setMarketType(value)} type="button" key={value}>{value === "futures" ? "合约" : "现货"}</button>)}</div>
        <div aria-label="单币时间跨度" className="flex gap-1" role="group">{SPANS.map((item) => <button aria-pressed={span === item.key} className={`h-6 rounded-sm px-2.5 font-mono text-[9px] font-semibold ${span === item.key ? "bg-surface-container text-text-primary" : "text-text-muted hover:text-text-primary"}`} onClick={() => setSpan(item.key)} type="button" key={item.key}>{item.key}</button>)}</div>
        <div className="ml-auto flex items-center gap-2"><span className={`h-1.5 w-1.5 rounded-full ${error ? "bg-risk" : loading || coinLoading ? "animate-pulse bg-warn" : "bg-good"}`} /><span className="max-w-64 truncate text-[9px] text-text-muted">{error || `${selected} · ${coin.data_status || "loading"}`}</span><button className="h-7 rounded-sm border border-border-subtle bg-surface-low px-2.5 text-[9px] font-semibold text-text-secondary" disabled={loading || coinLoading} onClick={() => { void loadOverview(true); void loadCoin(true); }} type="button">刷新</button></div>
      </section>

      <section className="grid shrink-0 grid-cols-2 gap-2 sm:grid-cols-3 min-[1160px]:h-[66px] min-[1160px]:grid-cols-6">
        {[
          ["现货净流", money(spotSummary.net_flow_usd), spotSummary.net_flow_usd, `${spotSummary.covered_assets || 0}/${spotSummary.asset_count || 0} 资产`],
          ["合约净流", money(futuresSummary.net_flow_usd), futuresSummary.net_flow_usd, `${futuresSummary.covered_assets || 0}/${futuresSummary.asset_count || 0} 资产`],
          ["当前价格", money(selectedAsset?.price, false), selectedAsset?.price_change_pct, percent(selectedAsset?.price_change_pct)],
          ["单所 OI", money(selectedAsset?.oi_usd, false), selectedAsset?.oi_change_pct, percent(selectedAsset?.oi_change_pct)],
          ["资金费率", percent(selectedAsset?.funding_pct, 4), selectedAsset?.funding_pct, "单周期"],
          ["跨所集中", percent(crossOi.top_exchange_share_pct), null, `${crossOi.coverage?.exchanges || 0}/${crossOi.coverage?.target || 3} 场所`]
        ].map(([label, value, raw, detail]) => <div className="workstation-panel px-2.5 py-2" key={String(label)}><div className="text-[9px] text-text-muted">{label}</div><div className={`table-number mt-0.5 truncate text-[13px] font-semibold ${tone(raw)}`}>{String(value)}</div><div className="truncate text-[8px] text-text-muted">{detail}</div></div>)}
      </section>

      <main className="grid min-h-0 flex-1 grid-cols-1 gap-2 min-[1160px]:grid-cols-[300px_minmax(0,1fr)_300px]">
        <section className="workstation-panel flex min-h-0 flex-col">
          <PanelHeader action={<span className="font-mono text-[9px] text-text-muted">{visibleAssets.length}</span>} detail="净流入排序 · 封闭 1h 窗口" title={`${marketType === "spot" ? "现货" : "合约"}资产`} />
          <div className="border-b border-border-subtle p-2"><input aria-label="筛选资金资产" className="h-7 w-full rounded-sm border border-border-subtle bg-surface-low px-2 text-[10px] uppercase text-text-primary outline-none focus:border-primary-500" onChange={(event) => setQuery(event.target.value.trim().toUpperCase())} placeholder="筛选 BTC" value={query} /></div>
          <AssetList assets={visibleAssets} onSelect={setSelected} selected={selected} />
        </section>

        <section className="grid min-h-0 gap-2 grid-rows-[minmax(300px,1.35fr)_minmax(210px,0.65fr)]">
          <div className="workstation-panel min-h-0 overflow-y-auto workstation-scroll">
            <PanelHeader action={<span className="text-[9px] text-text-muted">{coin.chart?.coverage?.returned || 0} 根</span>} detail={`${coin.chart?.source || "行情源待连接"} · ${coin.chart?.interval || "—"}`} title={`${selected} ${marketType === "spot" ? "现货" : "合约"}时序`} />
            <CandlestickChart points={coin.chart?.points || []} />
          </div>
          <div className="workstation-panel min-h-0 overflow-y-auto workstation-scroll">
            <PanelHeader detail="累计主动成交差（CVD）与价格，不代表充提净流入" title={`${marketType === "spot" ? "现货" : "合约"}累计资金`} />
            <FlowPriceChart marketType={marketType} points={series} />
          </div>
        </section>

        <aside className="grid min-h-0 gap-2 grid-rows-[210px_minmax(190px,1fr)_170px]">
          <section className="workstation-panel overflow-y-auto workstation-scroll"><PanelHeader action={<span className={`text-[8px] ${crossOi.data_status === "ready" ? "text-good" : "text-warn"}`}>{(crossOi.data_status || "loading").toUpperCase()}</span>} detail="Binance · Bybit · OKX" title="跨交易所 OI" /><CrossOi payload={crossOi} /></section>
          <section className="workstation-panel overflow-y-auto workstation-scroll"><PanelHeader detail={`历史样本 ${series.length} · 聚合快照`} title="OI / 费率历史" /><div className="grid gap-3 p-3"><MetricSeriesChart label="OI" metric="oi_usd" points={series} unit="usd" /><MetricSeriesChart label="资金费率" metric="funding_pct" points={series} unit="percent_per_cycle" /></div></section>
          <section className="workstation-panel"><PanelHeader detail="扫描资产横截面，不是持有人分布" title="资金集中度" /><div className="p-3">{[["Top 10 OI", topShare(10)], ["Top 50 OI", topShare(50)], ["单币最大场所", crossOi.top_exchange_share_pct]].map(([label, value]) => <div className="mb-3" key={String(label)}><div className="flex justify-between text-[9px]"><span className="text-text-muted">{label}</span><span className="font-mono text-text-primary">{percent(value)}</span></div><div className="mt-1 h-1 bg-surface-container"><div className="h-full bg-primary-500" style={{ width: `${Math.max(0, Math.min(100, Number(value || 0)))}%` }} /></div></div>)}</div></section>
        </aside>
      </main>
    </div>
  );
}
