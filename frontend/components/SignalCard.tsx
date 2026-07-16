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

function rankText(rank?: { available?: boolean; percentile?: number; rank?: number; sample_size?: number }) {
  if (!rank?.available) return "样本积累中";
  return `P${Math.round(Number(rank.percentile || 0))} · #${rank.rank}/${rank.sample_size}`;
}

export function SignalCard({
  item,
  context = "default",
  onOpen
}: {
  item: SignalItem;
  context?: "default" | "radar";
  onOpen?: (item: SignalItem) => void;
}) {
  const display = item.display || {};
  const symbol = item.symbol || item.coin || display.symbol_label || "";
  const cardTone = display.card_tone || toneForStatus(item.status);
  const score = display.score_label || item.score;
  const stage = display.stage_label || item.stage;
  const signalReference = item.public_ref || item.id;
  const intelligence = item.intelligence;
  const lifecycle = intelligence?.lifecycle;
  const resonanceWindows = intelligence?.resonance?.windows || [];

  return (
    <article className={`panel group overflow-hidden border-l-4 p-5 transition hover:border-primary-100 hover:border-l-primary-500 hover:bg-surface-bright ${accentClass(cardTone)}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <DataStatusBadge label={display.module_label || moduleLabel(item.module)} tone="info" />
          {lifecycle?.label ? <DataStatusBadge label={lifecycle.label} tone={lifecycle.state === "enhancing" ? "good" : lifecycle.state === "cooling" || lifecycle.state === "expired" ? "warn" : "neutral"} /> : null}
        </div>
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

      {intelligence ? (
        <div className="mt-4 rounded-xl border border-border-subtle bg-surface-bright p-3">
          <div className="grid grid-cols-2 gap-3 text-xs">
            <div><div className="text-text-muted">自身历史极端度</div><div className="table-number mt-1 font-semibold text-text-primary">{rankText(intelligence.self_rank)}</div></div>
            <div><div className="text-text-muted">市场相对强度</div><div className="table-number mt-1 font-semibold text-text-primary">{rankText(intelligence.market_strength_rank)}</div></div>
          </div>
          {resonanceWindows.length ? (
            <div className="mt-3 flex items-center gap-1.5" aria-label="跨模块信号共振">
              {resonanceWindows.map((window) => (
                <span className={`flex-1 rounded-md px-1 py-1.5 text-center text-[10px] font-semibold ${window.active ? "bg-primary-600 text-white" : "bg-surface-container text-text-muted"}`} key={window.key} title={window.active ? `${window.module_count} 个模块共振` : `${window.signal_count || 0} 条信号`}>
                  {window.key}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {context === "default" ? (
        <div className="mt-5 border-t border-border-subtle pt-4">
          <Link className="btn-secondary w-full justify-between px-3 group-hover:border-primary-100 group-hover:text-primary-700" href={signalReference ? `/radar?signal=${signalReference}` : "/radar"}>
            <span>查看信号雷达</span><span aria-hidden="true">→</span>
          </Link>
        </div>
      ) : (
        <div className="mt-5 border-t border-border-subtle pt-4">
          <button className="btn-secondary w-full justify-between px-3 group-hover:border-primary-100 group-hover:text-primary-700" disabled={!signalReference} onClick={() => onOpen?.(item)} type="button">
            <span>查看证据与上下文</span><span aria-hidden="true">→</span>
          </button>
        </div>
      )}
    </article>
  );
}
