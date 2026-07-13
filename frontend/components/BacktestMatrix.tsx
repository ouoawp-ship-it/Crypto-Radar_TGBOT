import type { BacktestMatrixPayload } from "@/lib/types";
import { pct, ratioPct, safeText } from "@/lib/format";
import { EmptyState } from "./EmptyState";

export function BacktestMatrix({ data }: { data?: BacktestMatrixPayload }) {
  const items = data?.items || [];
  const horizons = data?.horizons || ["1h", "4h", "24h", "72h"];
  if (!items.length) return <EmptyState title="暂无回测矩阵" text="等待更多结果追踪成功样本后会自动生成。" />;
  return (
    <div className="panel overflow-hidden">
      <div className="border-b border-border-subtle p-4 text-sm font-semibold text-text-primary">决策 x 周期矩阵</div>
      <div className="overflow-x-auto">
        <table className="min-w-full text-left text-sm">
          <thead className="bg-surface-bright text-xs uppercase text-text-muted">
            <tr>
              <th className="px-4 py-3">决策</th>
              {horizons.map((horizon) => (
                <th className="px-4 py-3" key={horizon}>
                  {horizon}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr className="border-t border-border-subtle hover:bg-surface-bright" key={row.decision_code || row.decision_label}>
                <td className="px-4 py-3 font-semibold text-text-primary">{safeText(row.decision_label || row.decision_code)}</td>
                {horizons.map((horizon) => {
                  const cell = row.horizons?.[horizon] || {};
                  return (
                    <td className="px-4 py-3 text-text-secondary" key={horizon}>
                      <div className="table-number font-semibold text-primary-700">{pct(cell.avg_final_return_pct)}</div>
                      <div className="text-xs text-text-muted">正收益 {ratioPct(cell.positive_ratio)} / 样本 {cell.success_count || 0}</div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
