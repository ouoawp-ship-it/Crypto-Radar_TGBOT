import type {
  ApiEnvelope,
  ApiResult,
  BacktestMatrixPayload,
  BacktestPayload,
  CoinItem,
  DecisionItem,
  ListPayload,
  OutcomeItem,
  SignalItem
} from "./types";

export type Query = Record<string, string | number | boolean | undefined | null>;

const INTERNAL_BASE = process.env.PAOXX_PUBLIC_API_INTERNAL_BASE || "http://127.0.0.1:8080";
const REQUEST_TIMEOUT_MS = Number(process.env.PAOXX_PUBLIC_API_TIMEOUT_MS || 15000);

function toQuery(query?: Query): string {
  const params = new URLSearchParams();
  Object.entries(query || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  });
  const text = params.toString();
  return text ? `?${text}` : "";
}

function publicApiUrl(path: `/public-api/${string}`, query?: Query): string {
  const suffix = `${path}${toQuery(query)}`;
  if (typeof window === "undefined") return `${INTERNAL_BASE}${suffix}`;
  return suffix;
}

function chineseError(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") return fallback;
  const record = payload as Record<string, unknown>;
  if (typeof record.message === "string" && record.message.trim()) return record.message;
  const error = record.error;
  if (typeof error === "string" && error.trim()) return error;
  if (error && typeof error === "object") {
    const message = (error as Record<string, unknown>).message;
    if (typeof message === "string" && message.trim()) return message;
  }
  return fallback;
}

function unwrapPayload<T>(payload: ApiEnvelope<T> & T): T {
  if (payload && typeof payload === "object" && "data" in payload && payload.data !== undefined) {
    return payload.data as T;
  }
  return payload as T;
}

export async function publicFetchResult<T>(path: `/public-api/${string}`, query?: Query): Promise<ApiResult<T>> {
  const url = publicApiUrl(path, query);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(url, { cache: "no-store", signal: controller.signal });
    const text = await res.text();
    let payload: (ApiEnvelope<T> & T) | null = null;
    try {
      payload = text ? (JSON.parse(text) as ApiEnvelope<T> & T) : ({} as ApiEnvelope<T> & T);
    } catch {
      return {
        ok: false,
        status: res.status,
        path,
        error: "公开接口返回格式异常，请稍后重试。"
      };
    }
    if (!res.ok || payload.ok === false) {
      return {
        ok: false,
        status: res.status,
        path,
        error: chineseError(payload, "公开接口暂时不可用，请稍后重试。")
      };
    }
    return { ok: true, status: res.status, path, data: unwrapPayload<T>(payload) };
  } catch (err) {
    const aborted = err instanceof DOMException && err.name === "AbortError";
    return {
      ok: false,
      path,
      error: aborted ? "公开接口响应超时，请稍后重试。" : "数据暂时不可用，请稍后重试。"
    };
  } finally {
    clearTimeout(timer);
  }
}

export async function publicFetch<T>(path: `/public-api/${string}`, query?: Query): Promise<T> {
  const result = await publicFetchResult<T>(path, query);
  if (!result.ok || result.data === undefined) throw new Error(result.error || "公开接口请求失败");
  return result.data;
}

export function getSignalStats(windowSec = 86400) {
  return publicFetch<Record<string, unknown>>("/public-api/signals/stats", { window_sec: windowSec });
}

export function getSignals(query: Query = {}) {
  return publicFetch<ListPayload<SignalItem>>("/public-api/signals", query);
}

export function getLatestSignals(query: Query = {}) {
  return publicFetch<ListPayload<SignalItem>>("/public-api/signals/latest", query).catch(() => getSignals(query));
}

export function getTimeline(query: Query = {}) {
  return publicFetch<ListPayload<SignalItem> & { groups?: Array<{ label?: string; items?: SignalItem[] }> }>("/public-api/signal-timeline", query);
}

export function getCoinSearch(query: Query = {}) {
  return publicFetch<ListPayload<CoinItem>>("/public-api/coin-search", query);
}

export function getDecisionStats(windowSec = 86400) {
  return publicFetch<Record<string, unknown>>("/public-api/decisions/stats", { window_sec: windowSec });
}

export function getDecisions(query: Query = {}) {
  return publicFetch<ListPayload<DecisionItem> & { decisions?: DecisionItem[]; distribution?: Record<string, unknown> }>("/public-api/decisions", query);
}

export function getDecision(symbol: string) {
  return publicFetch<DecisionItem>("/public-api/decision", { symbol });
}

export function getOutcomes(query: Query = {}) {
  return publicFetch<ListPayload<OutcomeItem>>("/public-api/outcomes", query);
}

export function getOutcomeStats(horizon = "1h") {
  return publicFetch<Record<string, unknown>>("/public-api/outcomes/stats", { horizon });
}

export function getSymbolOutcomes(symbol: string, query: Query = {}) {
  return publicFetch<ListPayload<OutcomeItem>>("/public-api/symbol-outcomes", { symbol, ...query });
}

export function getCoinDetail(symbol: string) {
  return publicFetch<Record<string, unknown>>("/public-api/coin-detail", { symbol });
}

export function getSymbolTimeline(symbol: string, query: Query = {}) {
  return publicFetch<ListPayload<SignalItem> & { groups?: Array<{ label?: string; items?: SignalItem[] }> }>("/public-api/signal-timeline", {
    symbol,
    limit: 80,
    ...query
  });
}

export function getBacktestDecision(query: Query = {}) {
  return publicFetch<BacktestPayload>("/public-api/backtest/decision", query);
}

export function getBacktestMatrix(query: Query = {}) {
  return publicFetch<BacktestMatrixPayload>("/public-api/backtest/decision/matrix", query);
}

export function getBacktestDetail(query: Query = {}) {
  return publicFetch<ListPayload<OutcomeItem>>("/public-api/backtest/decision/detail", query);
}

export type HomeDashboardData = {
  signalStats?: Record<string, unknown>;
  signals?: SignalItem[];
  coins?: CoinItem[];
  decisionStats?: Record<string, unknown>;
  decisions?: DecisionItem[];
  outcomeStats?: Record<string, unknown>;
  backtest?: BacktestPayload;
  matrix?: BacktestMatrixPayload;
  errors?: string[];
};

export async function loadHomeDashboardData(): Promise<HomeDashboardData> {
  const tasks = await Promise.allSettled([
    getSignalStats(),
    getSignals({ limit: 8, window_sec: 86400 }),
    getCoinSearch({ limit: 10, window_sec: 604800 }),
    getDecisionStats(86400),
    getDecisions({ limit: 6, window_sec: 86400 }),
    getOutcomeStats("1h"),
    getBacktestDecision({ horizon: "1h", window_sec: 2592000 }),
    getBacktestMatrix({ window_sec: 2592000 })
  ]);
  const errors: string[] = [];
  const value = <T>(index: number): T | undefined => {
    const item = tasks[index];
    if (item.status === "fulfilled") return item.value as T;
    errors.push(item.reason instanceof Error ? item.reason.message : "数据暂时不可用");
    return undefined;
  };
  const signalsPayload = value<ListPayload<SignalItem>>(1);
  const coinsPayload = value<ListPayload<CoinItem>>(2);
  const decisionsPayload = value<ListPayload<DecisionItem> & { decisions?: DecisionItem[] }>(4);
  return {
    signalStats: value<Record<string, unknown>>(0),
    signals: signalsPayload?.items || [],
    coins: coinsPayload?.items || [],
    decisionStats: value<Record<string, unknown>>(3),
    decisions: decisionsPayload?.items || decisionsPayload?.decisions || [],
    outcomeStats: value<Record<string, unknown>>(5),
    backtest: value<BacktestPayload>(6),
    matrix: value<BacktestMatrixPayload>(7),
    errors
  };
}
