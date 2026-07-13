export function DataStatusBadge({ label, tone = "neutral" }: { label?: string; tone?: "good" | "warn" | "bad" | "info" | "neutral" }) {
  const cls = {
    good: "border-good/25 bg-good/10 text-emerald-700",
    warn: "border-warn/25 bg-warn/10 text-amber-700",
    bad: "border-risk/25 bg-risk/10 text-red-700",
    info: "border-primary-500/25 bg-primary-50 text-primary-700",
    neutral: "border-border-subtle bg-surface-bright text-text-secondary"
  }[tone];
  return <span className={`inline-flex rounded-full border px-2.5 py-1 text-xs font-medium ${cls}`}>{label || "-"}</span>;
}
