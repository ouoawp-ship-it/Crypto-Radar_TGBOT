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
    <div className="panel p-4">
      <div className="text-xs font-semibold uppercase tracking-wide text-text-muted">{label}</div>
      <div className={`table-number mt-2 text-2xl font-semibold ${toneClass}`}>{safeText(value)}</div>
      {hint ? <div className="mt-2 text-xs text-text-muted">{hint}</div> : null}
    </div>
  );
}
