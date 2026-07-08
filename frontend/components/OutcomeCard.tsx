import type { OutcomeItem } from "@/lib/types";
import { pct, safeText } from "@/lib/format";
import { DataStatusBadge } from "./DataStatusBadge";

export function OutcomeCard({ item }: { item: OutcomeItem }) {
  const tone = item.result_tone || (item.data_status === "success" ? "info" : "neutral");
  return (
    <article className="panel p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-base font-black text-white">{safeText(item.symbol)}</div>
          <div className="text-xs text-slate-500">{safeText(item.signal_time)}</div>
        </div>
        <DataStatusBadge label={item.result_label || "数据不足"} tone={tone} />
      </div>
      <div className="mt-4 grid grid-cols-3 gap-3 text-sm">
        <div>
          <div className="text-xs text-slate-500">最终涨跌</div>
          <div className="font-black text-slate-100">{pct(item.final_return_pct)}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500">最高涨幅</div>
          <div className="font-black text-green-200">{pct(item.max_gain_pct)}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500">最大回撤</div>
          <div className="font-black text-amber-200">{pct(item.max_drawdown_pct)}</div>
        </div>
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        <DataStatusBadge label={`窗口 ${item.horizon || "-"}`} />
        <DataStatusBadge label={`决策 ${item.decision_label || "-"}`} tone="info" />
        <DataStatusBadge label={`状态 ${item.data_status || "-"}`} />
      </div>
    </article>
  );
}
