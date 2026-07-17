export function compact(value: unknown): string {
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value ?? "-");
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(num);
}

export function safeText(value: unknown, fallback = "-"): string {
  const text = String(value ?? "").trim();
  return text || fallback;
}

export function formatDateTime(value?: string | number | Date | null): string {
  if (!value) return "-";
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

export function toneForStatus(status?: string): "good" | "warn" | "bad" | "info" | "neutral" {
  if (status === "sent" || status === "success") return "good";
  if (status === "failed" || status === "error") return "bad";
  if (status === "blocked" || status === "unavailable") return "warn";
  if (status === "dry_run" || status === "pending" || status === "ready") return "info";
  return "neutral";
}

export function moduleLabel(value?: string): string {
  const map: Record<string, string> = {
    launch: "启动雷达",
    funding: "资金费率",
    flow: "资金流",
    announcement: "公告",
    summary: "资金摘要",
    test: "测试",
    ai: "AI 辅助"
  };
  return map[value || ""] || value || "其他";
}

export function statusLabel(value?: string): string {
  const map: Record<string, string> = {
    sent: "已发送",
    dry_run: "演练",
    skipped: "已跳过",
    blocked: "已阻止",
    failed: "失败"
  };
  return map[value || ""] || value || "未知";
}

export function formatMetricValue(value: unknown, unit?: string): string {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (unit === "percent" || unit === "percent_per_cycle") {
    const digits = Math.abs(number) < 0.1 ? 3 : 2;
    return `${number > 0 ? "+" : ""}${number.toFixed(digits)}%`;
  }
  if (unit === "ratio") return `${number.toFixed(number >= 10 ? 1 : 2)}×`;
  if (unit === "usd") {
    if (Math.abs(number) >= 1_000_000) return `$${compact(number)}`;
    if (Math.abs(number) >= 1) return `$${new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(number)}`;
    return `$${number.toPrecision(4)}`;
  }
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 }).format(number);
}

export function freshnessLabel(status?: string, ageSec?: number): string {
  const age = Math.max(0, Number(ageSec || 0));
  const ageText = age < 60 ? `${Math.round(age)} 秒前` : age < 3600 ? `${Math.round(age / 60)} 分钟前` : `${Math.round(age / 3600)} 小时前`;
  const prefix = status === "fresh" ? "实时" : status === "stale" ? "旧缓存" : status === "degraded" ? "部分数据" : "不可用";
  return `${prefix} · ${ageText}`;
}
