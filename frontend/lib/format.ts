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
    structure: "结构雷达",
    structure_review: "结构复盘",
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
