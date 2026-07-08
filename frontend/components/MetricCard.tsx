import { safeText } from "@/lib/format";

export function MetricCard({ label, value, hint, tone = "info" }: { label: string; value: unknown; hint?: string; tone?: "good" | "warn" | "bad" | "info" | "neutral" }) {
  const toneClass = {
    good: "text-good",
    warn: "text-warn",
    bad: "text-risk",
    info: "text-cyan-200",
    neutral: "text-slate-200"
  }[tone];
  return (
    <div className="panel p-4">
      <div className="text-xs font-bold text-slate-500">{label}</div>
      <div className={`mt-2 text-2xl font-black ${toneClass}`}>{safeText(value)}</div>
      {hint ? <div className="mt-2 text-xs text-slate-500">{hint}</div> : null}
    </div>
  );
}
