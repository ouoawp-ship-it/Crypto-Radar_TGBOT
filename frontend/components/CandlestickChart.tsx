import type { KlinePoint } from "@/lib/types";
import { formatDateTime, formatMetricValue } from "@/lib/format";

const WIDTH = 960;
const HEIGHT = 340;
const PRICE_TOP = 20;
const PRICE_BOTTOM = 255;
const VOLUME_TOP = 276;
const VOLUME_BOTTOM = 326;

export function CandlestickChart({ points }: { points: KlinePoint[] }) {
  const safe = points.filter((item) => [item.open, item.high, item.low, item.close].every((value) => Number.isFinite(Number(value))));
  if (!safe.length) return <div className="grid h-[300px] place-items-center text-[9px] text-text-muted">K 线数据暂不可用</div>;
  const minimum = Math.min(...safe.map((item) => Number(item.low)));
  const maximum = Math.max(...safe.map((item) => Number(item.high)));
  const range = Math.max(maximum - minimum, Math.abs(maximum) * 0.0001, 1e-12);
  const maxVolume = Math.max(1, ...safe.map((item) => Number(item.quote_volume || 0)));
  const slot = (WIDTH - 64) / safe.length;
  const candleWidth = Math.max(1.5, Math.min(8, slot * 0.62));
  const priceY = (value: number) => PRICE_BOTTOM - ((value - minimum) / range) * (PRICE_BOTTOM - PRICE_TOP);
  const gridValues = Array.from({ length: 5 }, (_, index) => maximum - (range * index) / 4);
  return <div className="overflow-hidden"><svg aria-label={`K 线图，${safe.length} 根 K 线`} className="h-auto min-h-[250px] w-full" role="img" viewBox={`0 0 ${WIDTH} ${HEIGHT}`}>
    {gridValues.map((value) => { const y = priceY(value); return <g key={value}><line className="stroke-border-subtle" strokeDasharray="4 5" x1="16" x2="888" y1={y} y2={y}/><text className="fill-text-muted text-[10px]" x="900" y={y + 4}>{formatMetricValue(value)}</text></g>; })}
    {safe.map((item, index) => {
      const x = 22 + slot * index + slot / 2;
      const open = Number(item.open); const close = Number(item.close); const high = Number(item.high); const low = Number(item.low);
      const rising = close >= open;
      const bodyTop = priceY(Math.max(open, close)); const bodyBottom = priceY(Math.min(open, close)); const bodyHeight = Math.max(1.5, bodyBottom - bodyTop);
      const volumeHeight = Number(item.quote_volume || 0) / maxVolume * (VOLUME_BOTTOM - VOLUME_TOP);
      return <g className={rising ? "text-good" : "text-risk"} key={`${item.open_time_ms || index}-${index}`}><title>{`${formatDateTime(item.open_time)} · O ${open} H ${high} L ${low} C ${close}`}</title><line className="stroke-current" x1={x} x2={x} y1={priceY(high)} y2={priceY(low)}/><rect className="fill-current" height={bodyHeight} rx="0.6" width={candleWidth} x={x - candleWidth / 2} y={bodyTop}/><rect className="fill-current opacity-25" height={volumeHeight} width={Math.max(1, candleWidth)} x={x - candleWidth / 2} y={VOLUME_BOTTOM - volumeHeight}/></g>;
    })}
    <line className="stroke-border-subtle" x1="16" x2="888" y1={VOLUME_TOP - 6} y2={VOLUME_TOP - 6}/><text className="fill-text-muted text-[10px]" x="20" y="338">{formatDateTime(safe[0]?.open_time)}</text><text className="fill-text-muted text-[10px]" textAnchor="end" x="888" y="338">{formatDateTime(safe[safe.length - 1]?.open_time)}</text>
  </svg></div>;
}
