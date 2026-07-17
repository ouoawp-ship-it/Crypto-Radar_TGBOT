import { safeText } from "@/lib/format";

export function MetricCard({
  label,
  value,
  hint,
  tone = "neutral"
}: {
  label: string;
  value: unknown;
  hint?: string;
  tone?: "good" | "warn" | "bad" | "info" | "neutral";
}) {
  const toneClass = {
    good: "text-good",
    warn: "text-warn",
    bad: "text-risk",
    info: "text-primary-700",
    neutral: "text-text-primary"
  }[tone];
  return (
    <div className="cockpit-panel min-w-0 p-3.5">
      <div className="flex items-center justify-between gap-2">
        <div className="text-[11px] font-semibold text-text-muted">{label}</div>
        {hint ? <div className="truncate text-[10px] text-text-muted">{hint}</div> : null}
      </div>
      <div className={`table-number mt-2 text-xl font-semibold ${toneClass}`}>{safeText(value)}</div>
    </div>
  );
}
