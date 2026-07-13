import Link from "next/link";
import type { SignalItem } from "@/lib/types";
import { formatDateTime, moduleLabel, safeText, statusLabel, toneForDecision, toneForStatus } from "@/lib/format";
import { DataStatusBadge } from "./DataStatusBadge";

export function SignalCard({ item }: { item: SignalItem }) {
  const display = item.display || {};
  const symbol = item.symbol || display.symbol_label || "";
  const decision = item.decision;
  return (
    <article className="panel p-4 transition hover:border-primary-100 hover:bg-surface-bright">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-text-primary">{safeText(display.title || item.signal_type, "信号卡片")}</div>
          <div className="mt-1 text-xs text-text-muted">{safeText(display.time_label || formatDateTime(item.time))}</div>
        </div>
        <DataStatusBadge label={display.status_label || statusLabel(item.status)} tone={toneForStatus(item.status)} />
      </div>
      <p className="mt-3 line-clamp-3 text-sm leading-6 text-text-secondary">{safeText(display.summary || item.excerpt, "暂无公开摘要。")}</p>
      <div className="mt-4 flex flex-wrap gap-2">
        <DataStatusBadge label={display.module_label || moduleLabel(item.module)} tone="info" />
        <DataStatusBadge label={symbol || "全局"} />
        <DataStatusBadge label={`分数 ${safeText(display.score_label || item.score)}`} />
        {display.stage_label || item.stage ? <DataStatusBadge label={`阶段 ${safeText(display.stage_label || item.stage)}`} /> : null}
        {decision?.label ? <DataStatusBadge label={`决策 ${decision.label}`} tone={toneForDecision(decision.code)} /> : null}
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        {symbol ? (
          <Link className="btn-secondary h-9" href={`/coin/${encodeURIComponent(symbol)}`}>
            币种详情
          </Link>
        ) : null}
        <Link className="btn-secondary h-9" href="/radar">
          查看信号流
        </Link>
      </div>
    </article>
  );
}
