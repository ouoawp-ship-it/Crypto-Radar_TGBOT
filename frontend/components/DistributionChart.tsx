function numericValue(value: unknown): number {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? Math.max(0, number) : 0;
}

export function DistributionChart({ data, valueKey = "count" }: { data: Array<Record<string, unknown>>; valueKey?: string }) {
  if (!data.length) {
    return <div className="panel p-5 text-sm text-text-muted">暂无分布数据。</div>;
  }

  const items = data.map((item, index) => ({
    label: String(item.label || item.name || index + 1),
    value: numericValue(item[valueKey])
  }));
  const maximum = Math.max(1, ...items.map((item) => item.value));

  return (
    <div className="panel h-72 p-4" role="list" aria-label="分布图">
      <div className="flex h-full flex-col justify-center gap-4 overflow-y-auto">
        {items.map((item, index) => {
          const width = item.value > 0 ? Math.max(3, (item.value / maximum) * 100) : 0;
          return (
            <div key={`${item.label}-${index}`} role="listitem" aria-label={`${item.label}: ${item.value}`}>
              <div className="mb-1.5 flex items-center justify-between gap-3 text-sm">
                <span className="truncate text-text-secondary" title={item.label}>{item.label}</span>
                <span className="table-number font-semibold text-text-primary">{item.value}</span>
              </div>
              <div className="h-2.5 overflow-hidden rounded-full bg-surface-low">
                <div
                  className="h-full rounded-full bg-primary-500 transition-[width] duration-300"
                  style={{ width: `${width}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
