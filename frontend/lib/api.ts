import type { AgentsOverviewPayload, ApiEnvelope, ApiResult, CoinContext, CrossExchangeOpenInterest, FundsAssetsPayload, FundsSectorsPayload, InfoFeedPayload, ListPayload, MarketOverview, MarketSnapshot, RadarBoards, RadarIntelligence, RealtimeIntelligencePayload, SignalContext, SignalItem, WatchlistMarketPayload } from "./types";

export type Query = Record<string, string | number | boolean | undefined | null>;
export type PublicFetchOptions = { bypassCache?: boolean; revalidateSec?: number };

const INTERNAL_BASE = process.env.PAOXX_PUBLIC_API_INTERNAL_BASE || "http://127.0.0.1:8080";
const REQUEST_TIMEOUT_MS = Number(process.env.PAOXX_PUBLIC_API_TIMEOUT_MS || 15000);
const MAX_RESPONSE_CACHE_ENTRIES = 100;
const responseCache = new Map<string, { expiresAt: number; result: ApiResult<unknown> }>();
const inFlight = new Map<string, { controller: AbortController; promise: Promise<unknown> }>();
let cacheGeneration = 0;

export function reportPublicTelemetry(event: "frontend_api_error" | "frontend_render_error" | "frontend_unhandled_error" | "frontend_route_loaded"): void {
  if (typeof window === "undefined") return;
  void fetch("/public-api/telemetry", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event }),
    keepalive: true,
    cache: "no-store"
  }).catch(() => undefined);
}

function toQuery(query?: Query): string {
  const params = new URLSearchParams();
  Object.entries(query || {}).sort(([a], [b]) => a.localeCompare(b)).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  });
  const text = params.toString();
  return text ? `?${text}` : "";
}

function publicUrl(path: `/public-api/${string}`, query?: Query): string {
  const suffix = `${path}${toQuery(query)}`;
  return typeof window === "undefined" ? `${INTERNAL_BASE}${suffix}` : suffix;
}

function errorText(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") return fallback;
  const value = payload as Record<string, unknown>;
  if (typeof value.message === "string" && value.message.trim()) return value.message;
  if (typeof value.error === "string" && value.error.trim()) return value.error;
  return fallback;
}

export function invalidatePublicApiCache(): void {
  cacheGeneration += 1;
  responseCache.clear();
  for (const entry of inFlight.values()) entry.controller.abort();
  inFlight.clear();
}

function cacheResponse(url: string, expiresAt: number, result: ApiResult<unknown>): void {
  responseCache.delete(url);
  responseCache.set(url, { expiresAt, result });
  while (responseCache.size > MAX_RESPONSE_CACHE_ENTRIES) {
    const oldest = responseCache.keys().next().value;
    if (!oldest) break;
    responseCache.delete(oldest);
  }
}

export async function publicFetchResult<T>(
  path: `/public-api/${string}`,
  query?: Query,
  options: PublicFetchOptions = {}
): Promise<ApiResult<T>> {
  const url = publicUrl(path, query);
  const ttl = Math.max(1, options.revalidateSec ?? (path.endsWith("/stats") ? 15 : 10));
  if (typeof window !== "undefined" && !options.bypassCache) {
    const cached = responseCache.get(url) as { expiresAt: number; result: ApiResult<T> } | undefined;
    if (cached && cached.expiresAt > Date.now()) return cached.result;
    if (cached) responseCache.delete(url);
  }
  if (options.bypassCache) {
    responseCache.delete(url);
    inFlight.get(url)?.controller.abort();
    inFlight.delete(url);
  }
  const pending = inFlight.get(url);
  if (pending) return pending.promise as Promise<ApiResult<T>>;

  const generation = cacheGeneration;
  const controller = new AbortController();
  const request = (async (): Promise<ApiResult<T>> => {
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(url, {
        cache: typeof window !== "undefined" || options.bypassCache ? "no-store" : undefined,
        next: typeof window === "undefined" && !options.bypassCache ? { revalidate: ttl } : undefined,
        signal: controller.signal
      });
      const raw = await response.text();
      const payload = (raw ? JSON.parse(raw) : {}) as ApiEnvelope<T> & T;
      if (!response.ok || payload.ok === false) {
        reportPublicTelemetry("frontend_api_error");
        return { ok: false, status: response.status, path, error: errorText(payload, "公开接口暂时不可用") };
      }
      const data = payload && typeof payload === "object" && "data" in payload && payload.data !== undefined
        ? payload.data as T
        : payload as T;
      const result: ApiResult<T> = { ok: true, status: response.status, path, data };
      if (typeof window !== "undefined" && generation === cacheGeneration) {
        cacheResponse(url, Date.now() + ttl * 1000, result);
      }
      return result;
    } catch (error) {
      const aborted = controller.signal.aborted || (error instanceof DOMException && error.name === "AbortError");
      if (!aborted || timedOut) reportPublicTelemetry("frontend_api_error");
      return { ok: false, path, error: timedOut ? "公开接口响应超时" : aborted ? "请求已取消" : "数据暂时不可用" };
    } finally {
      clearTimeout(timer);
    }
  })();
  const entry = { controller, promise: request };
  inFlight.set(url, entry);
  try {
    return await request;
  } finally {
    if (inFlight.get(url) === entry) inFlight.delete(url);
  }
}

export async function publicFetch<T>(path: `/public-api/${string}`, query?: Query, options: PublicFetchOptions = {}): Promise<T> {
  const result = await publicFetchResult<T>(path, query, options);
  if (!result.ok || result.data === undefined) throw new Error(result.error || "公开接口请求失败");
  return result.data;
}

export function getSignalStats(windowSec = 86400, options: PublicFetchOptions = {}) {
  return publicFetch<Record<string, unknown>>("/public-api/signals/stats", { window_sec: windowSec }, options);
}

export function getSignals(query: Query = {}, options: PublicFetchOptions = {}) {
  return publicFetch<ListPayload<SignalItem>>("/public-api/signals", query, options);
}

export function getSignalContext(signalId: number | string, options: PublicFetchOptions = {}) {
  return publicFetch<SignalContext>("/public-api/signals/context", { id: signalId }, { revalidateSec: 30, ...options });
}

export function getMarketSnapshot(symbol: string, options: PublicFetchOptions = {}) {
  return publicFetch<MarketSnapshot>("/public-api/market/snapshot", { symbol }, { revalidateSec: 30, ...options });
}

export function getMarketOverview(windowSec = 3600, options: PublicFetchOptions = {}) {
  return publicFetch<MarketOverview>("/public-api/market/overview", { window_sec: windowSec }, { revalidateSec: 15, ...options });
}

export function getRadarBoards(windowSec = 3600, limit = 8, options: PublicFetchOptions = {}) {
  return publicFetch<RadarBoards>("/public-api/radar/boards", { window_sec: windowSec, limit }, { revalidateSec: 15, ...options });
}

export function getWorkstationRadarMomentum(window: "15m" | "30m" | "1h" | "4h" | "1d", limit = 8, options: PublicFetchOptions = {}) {
  return publicFetch<RadarBoards & { window?: string }>("/public-api/workstation/radar/momentum", { window, limit }, { revalidateSec: 15, ...options });
}

export function getWorkstationRadarMomentumWindows(limit = 8, options: PublicFetchOptions = {}) {
  return publicFetch<{ windows: Record<string, RadarBoards & { window?: string }> }>(
    "/public-api/workstation/radar/momentum-windows",
    { limit },
    { revalidateSec: 15, ...options }
  );
}

export function getRealtimeIntelligence(limit = 30, options: PublicFetchOptions = {}) {
  return publicFetch<RealtimeIntelligencePayload>("/public-api/radar/realtime-intelligence", { limit }, { revalidateSec: 15, ...options });
}

export function getFundsSectors(windowSec = 3600, marketType: "spot" | "futures" = "spot", options: PublicFetchOptions = {}) {
  return publicFetch<FundsSectorsPayload>("/public-api/funds/sectors", { window_sec: windowSec, market_type: marketType }, { revalidateSec: 30, ...options });
}

export function getFundsAssets(query: Query = {}, options: PublicFetchOptions = {}) {
  return publicFetch<FundsAssetsPayload>("/public-api/funds/assets", query, { revalidateSec: 30, ...options });
}

export function getWorkstationFundsOpenInterest(symbol: string, options: PublicFetchOptions = {}) {
  return publicFetch<CrossExchangeOpenInterest>("/public-api/workstation/funds/open-interest", { symbol }, { revalidateSec: 30, ...options });
}

export function getRadarIntelligence(
  windowSec = 86400,
  limit = 5,
  signalRefs: Array<number | string> = [],
  options: PublicFetchOptions = {}
) {
  const refs = signalRefs.map((value) => String(value || "").trim()).filter(Boolean).slice(0, 40).join(",");
  return publicFetch<RadarIntelligence>("/public-api/radar/intelligence", { window_sec: windowSec, limit, refs }, { revalidateSec: 15, ...options });
}

export function getCoinContext(symbol: string, options: PublicFetchOptions = {}, chart: Query = {}) {
  return publicFetch<CoinContext>("/public-api/coin/context", { symbol, ...chart }, { revalidateSec: 30, ...options });
}

export function getWatchlistMarket(symbols: string[], options: PublicFetchOptions = {}) {
  return publicFetch<WatchlistMarketPayload>("/public-api/market/watchlist", { symbols: symbols.join(",") }, { revalidateSec: 30, ...options });
}

export function getInfoFeed(query: Query = {}, options: PublicFetchOptions = {}) {
  return publicFetch<InfoFeedPayload>("/public-api/info/feed", query, { revalidateSec: 60, ...options });
}

export function getAgentsOverview(windowSec = 14_400, options: PublicFetchOptions = {}) {
  return publicFetch<AgentsOverviewPayload>("/public-api/agents/overview", { window_sec: windowSec }, { revalidateSec: 120, ...options });
}

export type HomeDashboardData = {
  signalStats?: Record<string, unknown>;
  signals?: SignalItem[];
  error?: string;
};

export async function loadHomeDashboardData(): Promise<HomeDashboardData> {
  const [stats, signals] = await Promise.allSettled([
    getSignalStats(86400),
    getSignals({ limit: 8, window_sec: 86400 })
  ]);
  return {
    signalStats: stats.status === "fulfilled" ? stats.value : undefined,
    signals: signals.status === "fulfilled" ? signals.value.items || [] : [],
    error: stats.status === "rejected" && signals.status === "rejected" ? "公开信号暂时不可用" : undefined
  };
}
