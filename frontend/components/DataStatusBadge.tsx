export function DataStatusBadge({ label, tone = "neutral" }: { label?: string; tone?: "good" | "warn" | "bad" | "info" | "neutral" }) {
  const cls = {
    good: "border-good/30 bg-good/10 text-green-200",
    warn: "border-warn/30 bg-warn/10 text-amber-200",
    bad: "border-risk/30 bg-risk/10 text-red-200",
    info: "border-cyanline/30 bg-cyanline/10 text-cyan-100",
    neutral: "border-white/10 bg-white/5 text-slate-300"
  }[tone];
  return <span className={`inline-flex rounded-full border px-2.5 py-1 text-xs font-bold ${cls}`}>{label || "-"}</span>;
}
