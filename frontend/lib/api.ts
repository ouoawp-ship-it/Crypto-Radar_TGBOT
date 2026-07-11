import type {
  ApiEnvelope,
  ApiResult,
  BacktestMatrixPayload,
  BacktestPayload,
  CoinItem,
  DecisionItem,
  ListPayload,
  LifecycleDetailPayload,
  LifecycleIntelligenceDetailPayload,
  LifecycleIntelligenceItem,
  LifecycleIntelligenceSummaryPayload,
  LifecycleItem,
  LifecycleReplayFrame,
  LifecycleReplayPayload,
  LifecycleSimilarityPayload,
  LifecycleSummaryPayload,
  OutcomeItem,
  SignalItem
} from "./types";

export type Query = Record<string, string | number | boolean | undefined | null>;

export type PublicFetchOptions = {
  /** Bypass and refresh both the Next.js and browser short caches. */
  bypassCache?: boolean;
  /** Override the endpoint's short server-side cache window. */
  revalidateSec?: number;
};

const INTERNAL_BASE = process.env.PAOXX_PUBLIC_API_INTERNAL_BASE || "http://127.0.0.1:8080";
const REQUEST_TIMEOUT_MS = Number(process.env.PAOXX_PUBLIC_API_TIMEOUT_MS || 15000);
const MAX_CLIENT_CACHE_ENTRIES = 128;
const inFlightPublicRequests = new Map<string, Promise<unknown>>();
const clientPublicResponses = new Map<string, { expiresAt: number; result: ApiResult<unknown> }>();
let clientCacheGeneration = 0;

function pruneClientPublicResponses(now: number): void {
  for (const [key, entry] of clientPublicResponses) {
    if (entry.expiresAt <= now) clientPublicResponses.delete(key);
  }
  while (clientPublicResponses.size > MAX_CLIENT_CACHE_ENTRIES) {
    const oldest = clientPublicResponses.keys().next().value as string | undefined;
    if (!oldest) break;
    clientPublicResponses.delete(oldest);
  }
}

export function invalidatePublicApiCache(pathPrefix?: `/public-api/${string}`): void {
  clientCacheGeneration += 1;
  if (!pathPrefix) {
    clientPublicResponses.clear();
    inFlightPublicRequests.clear();
    return;
  }
  for (const key of clientPublicResponses.keys()) {
    if (key.startsWith(pathPrefix)) clientPublicResponses.delete(key);
  }
  for (const key of inFlightPublicRequests.keys()) {
    if (key.startsWith(pathPrefix)) inFlightPublicRequests.delete(key);
  }
}

function toQuery(query?: Query): string {
  const params = new URLSearchParams();
  Object.entries(query || {})
    .sort(([left], [right]) => left.localeCompare(right))
    .forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
    });
  const text = params.toString();
  return text ? `?${text}` : "";
}

function publicApiRevalidateSec(path: `/public-api/${string}`): number {
  if (path.startsWith("/public-api/backtest/")) return 30;
  if (path === "/public-api/outcomes/stats") return 30;
  if (path.endsWith("/stats")) return 15;
  return 10;
}

function publicRequestInit(
  path: `/public-api/${string}`,
  signal: AbortSignal,
  options: PublicFetchOptions
): RequestInit & { next?: { revalidate: number } } {
  // Browser TTL reuse is handled by this module; its network fetch remains
  // no-store so an explicit refresh reaches the public API. Private /api paths
  // cannot be passed to this typed client.
  if (typeof window !== "undefined" || options.bypassCache) {
    return { cache: "no-store", signal };
  }
  return {
    next: { revalidate: Math.max(1, options.revalidateSec ?? publicApiRevalidateSec(path)) },
    signal
  };
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

async function runPublicRequest<T>(
  path: `/public-api/${string}`,
  url: string,
  options: PublicFetchOptions
): Promise<ApiResult<T>> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(url, publicRequestInit(path, controller.signal, options));
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

export async function publicFetchResult<T>(
  path: `/public-api/${string}`,
  query?: Query,
  options: PublicFetchOptions = {}
): Promise<ApiResult<T>> {
  if (!path.startsWith("/public-api/")) {
    return { ok: false, status: 400, path, error: "仅允许请求公开只读接口" };
  }
  const url = publicApiUrl(path, query);
  const ttl = Math.max(1, options.revalidateSec ?? publicApiRevalidateSec(path));
  const clientCacheKey = `${url}|ttl:${ttl}`;
  if (typeof window !== "undefined") {
    if (options.bypassCache) invalidatePublicApiCache(path);
    const now = Date.now();
    pruneClientPublicResponses(now);
    if (!options.bypassCache) {
      const cached = clientPublicResponses.get(clientCacheKey) as { expiresAt: number; result: ApiResult<T> } | undefined;
      if (cached && cached.expiresAt > now) return cached.result;
    }
  }
  const cacheGeneration = clientCacheGeneration;
  const requestKey = `${url}|${options.bypassCache ? "bypass" : `ttl:${ttl}`}`;
  const pending = inFlightPublicRequests.get(requestKey) as Promise<ApiResult<T>> | undefined;
  if (pending) return pending;

  const request = runPublicRequest<T>(path, url, options);
  inFlightPublicRequests.set(requestKey, request);
  try {
    const result = await request;
    if (typeof window !== "undefined" && result.ok && cacheGeneration === clientCacheGeneration) {
      clientPublicResponses.delete(clientCacheKey);
      clientPublicResponses.set(clientCacheKey, {
        expiresAt: Date.now() + ttl * 1000,
        result: result as ApiResult<unknown>
      });
      pruneClientPublicResponses(Date.now());
    }
    return result;
  } finally {
    if (inFlightPublicRequests.get(requestKey) === request) inFlightPublicRequests.delete(requestKey);
  }
}

export async function publicFetch<T>(
  path: `/public-api/${string}`,
  query?: Query,
  options: PublicFetchOptions = {}
): Promise<T> {
  const result = await publicFetchResult<T>(path, query, options);
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
  return getSignals(query);
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

export function getLifecycleSummary() {
  return publicFetch<LifecycleSummaryPayload>("/public-api/lifecycle/summary");
}

export function getLifecycles(query: Query = {}) {
  return publicFetch<ListPayload<LifecycleItem>>("/public-api/lifecycle/list", query);
}

export function getLifecycleDetail(symbol: string) {
  return publicFetch<LifecycleDetailPayload>("/public-api/lifecycle/detail", { symbol });
}

export function getLifecycleEvents(symbol: string, query: Query = {}) {
  return publicFetch<ListPayload<Record<string, unknown>>>("/public-api/lifecycle/events", { symbol, ...query });
}

export function getLifecycleMetrics(symbol: string, query: Query = {}) {
  return publicFetch<ListPayload<Record<string, unknown>>>("/public-api/lifecycle/metrics", { symbol, ...query });
}

export function getLifecycleIntelligenceSummary() {
  return publicFetch<LifecycleIntelligenceSummaryPayload>("/public-api/lifecycle/intelligence/summary");
}

export function getLifecycleIntelligenceList(query: Query = {}) {
  return publicFetch<ListPayload<LifecycleIntelligenceItem>>("/public-api/lifecycle/intelligence/list", query);
}

export function getLifecycleIntelligenceDetail(symbol: string) {
  return publicFetch<LifecycleIntelligenceDetailPayload>("/public-api/lifecycle/intelligence/detail", { symbol });
}

export function getLifecycleReplay(symbol: string) {
  return publicFetch<LifecycleReplayPayload>("/public-api/lifecycle/replay", { symbol });
}

export function getLifecycleReplayFrames(symbol: string, query: Query = {}) {
  return publicFetch<ListPayload<LifecycleReplayFrame>>("/public-api/lifecycle/replay/frames", { symbol, ...query });
}

export function getLifecycleAnalytics(dimension: "first-level" | "upgrade-path" | "module" | "capital-confirmation") {
  return publicFetch<ListPayload<Record<string, unknown>> & {
    summary?: Record<string, unknown>;
    model_data_warnings?: string[];
    status?: string;
    message?: string;
  }>(`/public-api/lifecycle/analytics/${dimension}`);
}

export function getLifecycleSimilar(symbol: string, limit = 10) {
  return publicFetch<LifecycleSimilarityPayload>("/public-api/lifecycle/similar", { symbol, limit });
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
  lifecycle?: LifecycleSummaryPayload;
  lifecycleIntelligence?: LifecycleIntelligenceSummaryPayload;
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
    getBacktestMatrix({ window_sec: 2592000 }),
    getLifecycleSummary(),
    getLifecycleIntelligenceSummary()
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
    lifecycle: value<LifecycleSummaryPayload>(8),
    lifecycleIntelligence: value<LifecycleIntelligenceSummaryPayload>(9),
    errors
  };
}
