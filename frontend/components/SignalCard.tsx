import Link from "next/link";
import type { SignalItem, Tone } from "@/lib/types";
import { formatDateTime, moduleLabel, safeText, statusLabel, toneForStatus } from "@/lib/format";
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
  const valueText = String(value ?? "").trim();
  return Boolean(valueText && valueText !== "-" && valueText !== "—");
}

export function SignalCard({ item, context = "default" }: { item: SignalItem; context?: "default" | "radar" }) {
  const display = item.display || {};
  const symbol = item.symbol || item.coin || display.symbol_label || "";
  const cardTone = display.card_tone || toneForStatus(item.status);
  const score = display.score_label || item.score;
  const stage = display.stage_label || item.stage;

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
        {hasMeaningfulValue(score) ? (
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

      {hasMeaningfulValue(stage) ? (
        <div className="mt-4 text-xs text-text-muted">
          阶段 <strong className="font-semibold text-text-secondary">{safeText(stage)}</strong>
        </div>
      ) : null}

      {context === "default" ? (
        <div className="mt-5 border-t border-border-subtle pt-4">
          <Link className="btn-secondary w-full justify-between px-3 group-hover:border-primary-100 group-hover:text-primary-700" href="/radar">
            <span>查看信号雷达</span><span aria-hidden="true">→</span>
          </Link>
        </div>
      ) : null}
    </article>
  );
}
