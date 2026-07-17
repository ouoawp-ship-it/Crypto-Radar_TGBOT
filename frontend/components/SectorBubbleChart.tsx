"use client";

import type { SectorFlow } from "@/lib/types";
import { formatMetricValue, safeText } from "@/lib/format";

function bubbleSize(value: number, maximum: number) {
  if (maximum <= 0 || value <= 0) return 64;
  return Math.round(64 + Math.sqrt(value / maximum) * 78);
}

function tone(value?: number | null) {
  if (value == null) return "border-dashed border-border-subtle bg-surface-container-low text-text-muted";
  if (value > 0) return "border-good/30 bg-good/10 text-emerald-700";
  if (value < 0) return "border-risk/30 bg-risk/10 text-red-700";
  return "border-border-subtle bg-surface-container text-text-secondary";
}

export function SectorBubbleChart({
  sectors,
  selected,
  onSelect
}: {
  sectors: SectorFlow[];
  selected?: string;
  onSelect: (sector: string) => void;
}) {
  const visible = sectors.slice(0, 18);
  const maximum = Math.max(0, ...visible.map((item) => Math.abs(Number(item.magnitude_usd || 0))));
  const hasFlow = visible.some((item) => item.net_flow_usd !== null && item.net_flow_usd !== undefined);

  if (!visible.length) {
    return <div className="grid min-h-56 place-items-center px-6 text-center text-sm text-text-muted">板块资金正在积累有效扫描。</div>;
  }
  if (!hasFlow) {
    return <div className="grid min-h-[330px] place-items-center px-6 text-center"><div><div className="text-sm font-semibold text-text-primary">当前窗口尚无板块资金样本</div><p className="mx-auto mt-2 max-w-sm text-xs leading-5 text-text-muted">资金流雷达完成封闭窗口扫描后，这里会按版本化分类生成流入与流出气泡；未覆盖资产不按 0 计算。</p></div></div>;
  }

  return (
    <>
      <div aria-label="板块资金气泡图" className="hidden min-h-[330px] flex-wrap content-center items-center justify-center gap-3 p-4 md:flex">
        {visible.map((item) => {
          const id = safeText(item.sector_id, "other");
          const size = bubbleSize(Math.abs(Number(item.magnitude_usd || 0)), maximum);
          return (
            <button
              aria-pressed={selected === id}
              className={`group grid shrink-0 place-items-center rounded-full border text-center shadow-soft transition hover:-translate-y-0.5 hover:shadow-floating ${tone(item.net_flow_usd)} ${selected === id ? "ring-2 ring-primary-500 ring-offset-2 ring-offset-surface-panel" : ""}`}
              key={id}
              onClick={() => onSelect(selected === id ? "" : id)}
              style={{ height: size, width: size }}
              title={`${safeText(item.label)} · 覆盖 ${item.covered_assets || 0}/${item.asset_count || 0}`}
              type="button"
            >
              <span className="max-w-[88%] px-1">
                <span className="block truncate text-xs font-semibold">{safeText(item.label)}</span>
                <span className="table-number mt-1 block text-[11px] font-medium">{item.net_flow_usd == null ? "未覆盖" : formatMetricValue(item.net_flow_usd, "usd")}</span>
              </span>
            </button>
          );
        })}
      </div>

      <div className="grid gap-2 p-3 md:hidden">
        {visible.slice(0, 10).map((item) => {
          const id = safeText(item.sector_id, "other");
          return (
            <button className={`flex items-center justify-between rounded-lg border px-3 py-3 text-left ${selected === id ? "border-primary-500 bg-primary-50" : "border-border-subtle bg-surface-panel"}`} key={id} onClick={() => onSelect(selected === id ? "" : id)} type="button">
              <span><span className="block text-sm font-semibold text-text-primary">{safeText(item.label)}</span><span className="mt-0.5 block text-[11px] text-text-muted">覆盖 {item.covered_assets || 0}/{item.asset_count || 0}</span></span>
              <span className={`table-number text-sm font-semibold ${Number(item.net_flow_usd || 0) > 0 ? "text-emerald-700" : Number(item.net_flow_usd || 0) < 0 ? "text-red-700" : "text-text-muted"}`}>{item.net_flow_usd == null ? "—" : formatMetricValue(item.net_flow_usd, "usd")}</span>
            </button>
          );
        })}
      </div>
    </>
  );
}
