export function pct(value: unknown, digits = 2): string {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return `${num.toFixed(digits)}%`;
}

export function ratioPct(value: unknown): string {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return `${Math.round(num * 100)}%`;
}

export function compact(value: unknown): string {
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value ?? "-");
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(num);
}

export function safeText(value: unknown, fallback = "-"): string {
  const text = String(value ?? "").trim();
  return text || fallback;
}

export function toneForDecision(code?: string): "good" | "warn" | "bad" | "info" | "neutral" {
  if (code === "probe") return "good";
  if (code === "risk_alert") return "bad";
  if (code === "avoid_chase" || code === "wait_pullback") return "warn";
  if (code === "observe") return "info";
  return "neutral";
}

export function decisionLabel(code?: string, fallback?: string): string {
  const map: Record<string, string> = {
    observe: "观察",
    wait_pullback: "等待回踩",
    probe: "可试仓",
    avoid_chase: "禁止追高",
    risk_alert: "风险警报",
    unknown: "未识别"
  };
  return map[code || ""] || fallback || "未识别";
}
