export function pct(value: unknown, digits = 2): string {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return `${num.toFixed(digits)}%`;
}

export function ratioPct(value: unknown, digits = 1): string {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return `${(num * 100).toFixed(digits)}%`;
}

export function compact(value: unknown): string {
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value ?? "-");
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(num);
}

export function numberText(value: unknown, digits = 2): string {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(num);
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

export function normalizeSymbol(value: string): string {
  const upper = value.trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (!upper) return "";
  return upper.endsWith("USDT") ? upper : `${upper}USDT`;
}

export function toneForDecision(code?: string): "good" | "warn" | "bad" | "info" | "neutral" {
  if (code === "probe") return "good";
  if (code === "risk_alert") return "bad";
  if (code === "avoid_chase" || code === "wait_pullback") return "warn";
  if (code === "observe") return "info";
  return "neutral";
}

export function toneForStatus(status?: string): "good" | "warn" | "bad" | "info" | "neutral" {
  if (status === "sent" || status === "success") return "good";
  if (status === "failed" || status === "error") return "bad";
  if (status === "blocked" || status === "unavailable") return "warn";
  if (status === "dry_run" || status === "pending" || status === "ready") return "info";
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
    failed: "失败",
    pending: "待计算",
    ready: "待计算",
    success: "已计算",
    unavailable: "数据不足",
    error: "错误"
  };
  return map[value || ""] || value || "未知";
}
