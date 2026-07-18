import type { CoinSeriesPoint } from "@/lib/types";
import { formatMetricValue } from "@/lib/format";

export function MetricSeriesChart({ points, metric, label, unit }: { points: CoinSeriesPoint[]; metric: keyof CoinSeriesPoint; label: string; unit?: string }) {
  const values = points.map((item, index) => ({ index, value: Number(item[metric]) })).filter((item) => Number.isFinite(item.value));
  if (values.length < 2) return <div className="grid h-20 place-items-center text-[8px] text-text-muted">历史快照样本不足</div>;
  const min = Math.min(...values.map((item) => item.value)); const max = Math.max(...values.map((item) => item.value));
  const range = Math.max(max - min, Math.abs(max) * 0.0001, 1e-9);
  const x = (index: number) => 8 + index / Math.max(1, points.length - 1) * 284;
  const y = (value: number) => 92 - (value - min) / range * 76;
  const path = values.map((item, index) => `${index ? "L" : "M"}${x(item.index).toFixed(1)},${y(item.value).toFixed(1)}`).join(" ");
  const latest = values[values.length - 1]?.value;
  return <div><div className="flex items-baseline justify-between gap-3"><span className="text-[8px] font-semibold text-text-secondary">{label}</span><span className="table-number text-[10px] font-semibold text-text-primary">{formatMetricValue(latest, unit)}</span></div><svg aria-label={`${label}历史曲线`} className="mt-1 h-20 w-full" role="img" viewBox="0 0 300 108"><line className="stroke-border-subtle" strokeDasharray="3 4" x1="8" x2="292" y1="54" y2="54"/><path className="fill-none stroke-primary-600" d={path} strokeLinecap="round" strokeLinejoin="round" strokeWidth="2"/></svg></div>;
}
