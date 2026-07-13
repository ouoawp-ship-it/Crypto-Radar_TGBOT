import Link from "next/link";
import type { SignalItem, Tone } from "@/lib/types";
import { formatDateTime, moduleLabel, safeText, statusLabel, toneForDecision, toneForStatus } from "@/lib/format";
import { DataStatusBadge } from "./DataStatusBadge";

function accentClass(tone: Tone) {
  return {
    good: "border-l-good",
    warn: "border-l-warn",
    bad: "border-l-risk",
    info: "border-l-primary-500",
    neutral: "border-l-border-subtle"
  }[tone];
}

function hasMeaningfulValue(value: unknown) {
  const text = String(value ?? "").trim();
  return Boolean(text && text !== "-" && text !== "—");
}

export function SignalCard({ item, context = "default" }: { item: SignalItem; context?: "default" | "radar" }) {
  const display = item.display || {};
  const symbol = item.symbol || item.coin || display.symbol_label || "";
  const decision = item.decision;
  const decisionTone = toneForDecision(decision?.code);
  const cardTone = decision?.code ? decisionTone : display.card_tone || toneForStatus(item.status);
  const score = display.score_label || item.score;
  const hasScore = hasMeaningfulValue(score);
  const stage = display.stage_label || item.stage;
  const hasStage = hasMeaningfulValue(stage);
  const hasRisk = hasMeaningfulValue(decision?.risk_level);

  return (
    <article className={`panel group overflow-hidden border-l-4 p-5 transition hover:border-primary-100 hover:border-l-primary-500 hover:bg-surface-bright ${accentClass(cardTone)}`}>
      <div className="flex items-start justify-between gap-3">
        <DataStatusBadge label={display.module_label || moduleLabel(item.module)} tone="info" />
        <DataStatusBadge label={display.status_label || statusLabel(item.status)} tone={toneForStatus(item.status)} />
      </div>

      <div className="mt-5 flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className="table-number truncate text-xl font-semibold text-text-primary">{symbol || "全局信号"}</h3>
          <p className="mt-1 text-xs text-text-muted">{safeText(display.time_label || formatDateTime(item.time))}</p>
        </div>
        {hasScore ? (
          <div className="shrink-0 rounded-lg bg-surface-container-low px-3 py-2 text-right">
            <div className="text-[11px] font-semibold text-text-muted">信号分数</div>
            <div className="table-number mt-0.5 text-lg font-semibold text-text-primary">{safeText(score)}</div>
          </div>
        ) : null}
      </div>

      <div className="mt-4">
        <div className="text-sm font-semibold leading-6 text-text-primary">{safeText(display.title || item.signal_type, "市场信号")}</div>
        <p className="mt-1.5 line-clamp-3 min-h-[3rem] text-sm leading-6 text-text-secondary">{safeText(display.summary || item.excerpt, "暂无公开摘要。")}</p>
      </div>

      {decision?.label || decision?.summary ? (
        <div className="mt-4 rounded-lg border border-border-subtle bg-surface-container-low p-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-semibold text-text-muted">模型结论</span>
            {decision?.label ? <DataStatusBadge label={decision.label} tone={decisionTone} /> : null}
          </div>
          {decision?.summary ? <p className="mt-2 line-clamp-2 text-sm leading-5 text-text-secondary">{decision.summary}</p> : null}
        </div>
      ) : null}

      {hasStage || hasRisk ? (
        <div className="mt-4 flex min-h-7 flex-wrap items-center gap-x-4 gap-y-2 text-xs text-text-muted">
          {hasStage ? <span>阶段 <strong className="font-semibold text-text-secondary">{safeText(stage)}</strong></span> : null}
          {hasRisk ? <span>风险 <strong className="font-semibold text-text-secondary">{safeText(decision?.risk_level)}</strong></span> : null}
        </div>
      ) : null}

      <div className="mt-5 flex flex-col gap-2 border-t border-border-subtle pt-4 sm:flex-row sm:items-center">
        {symbol ? (
          <Link className="btn-secondary w-full justify-between px-3 group-hover:border-primary-100 group-hover:text-primary-700 sm:flex-1" href={`/coin/${encodeURIComponent(symbol)}`}>
            <span>{context === "radar" ? "打开币种分析" : "币种详情"}</span>
            <span aria-hidden="true">→</span>
          </Link>
        ) : null}
        {context === "default" ? (
          <Link className="btn-secondary w-full justify-between px-3 sm:flex-1" href="/radar">
            <span>查看信号流</span>
            <span aria-hidden="true">→</span>
          </Link>
        ) : null}
      </div>
    </article>
  );
}
