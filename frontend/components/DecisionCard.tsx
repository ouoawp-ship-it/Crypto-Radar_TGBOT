import Link from "next/link";
import type { DecisionItem } from "@/lib/types";
import { decisionLabel, safeText, toneForDecision } from "@/lib/format";
import { DataStatusBadge } from "./DataStatusBadge";

export function DecisionCard({ item }: { item: DecisionItem }) {
  const decision = item.decision || {};
  const code = decision.code || "unknown";
  const symbol = item.symbol || "-";
  return (
    <article className="panel p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-lg font-black text-white">{symbol}</div>
          <div className="text-xs text-slate-500">{item.coin || "USDT 交易对"}</div>
        </div>
        <DataStatusBadge label={decision.label || decisionLabel(code)} tone={toneForDecision(code)} />
      </div>
      <p className="mt-3 text-sm leading-6 text-slate-300">{safeText(decision.summary, "暂无决策摘要。")}</p>
      <div className="mt-4 flex flex-wrap gap-2">
        <DataStatusBadge label={`置信度 ${decision.confidence ?? 0}`} tone="info" />
        <DataStatusBadge label={`风险等级 ${decision.risk_level || "低"}`} tone={decision.risk_level === "高" ? "bad" : decision.risk_level === "中" ? "warn" : "good"} />
      </div>
      <div className="mt-4 text-xs leading-5 text-slate-400">
        <div>主要依据：{safeText((item.reasons || []).slice(0, 2).join("；"), "等待更多信号。")}</div>
        <div>观察点：{safeText((item.watch_points || []).slice(0, 2).join("；"), "等待下一轮确认。")}</div>
      </div>
      {symbol !== "-" ? (
        <Link className="btn mt-4 inline-flex" href={`/coin/${encodeURIComponent(symbol)}`}>
          查看单币
        </Link>
      ) : null}
    </article>
  );
}
