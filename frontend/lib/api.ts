"use client";

import type {
  ApiEnvelope,
  BacktestMatrixPayload,
  BacktestPayload,
  DecisionItem,
  OutcomeItem,
  SignalItem
} from "./types";

type Query = Record<string, string | number | undefined | null>;

function toQuery(query?: Query): string {
  const params = new URLSearchParams();
  Object.entries(query || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  });
  const text = params.toString();
  return text ? `?${text}` : "";
}

async function publicFetch<T>(path: `/public-api/${string}`, query?: Query): Promise<T> {
  const res = await fetch(`${path}${toQuery(query)}`, { cache: "no-store" });
  const payload = (await res.json()) as ApiEnvelope<T> & T;
  if (!res.ok || payload.ok === false) {
    const message = typeof payload.message === "string" ? payload.message : "公开接口请求失败";
    throw new Error(message);
  }
  return ((payload.data && typeof payload.data === "object" ? payload.data : payload) as T);
}

export function getSignalStats(windowSec = 86400) {
  return publicFetch<Record<string, unknown>>("/public-api/signals/stats", { window_sec: windowSec });
}

export function getSignals(query: Query = {}) {
  return publicFetch<{ items?: SignalItem[]; count?: number }>("/public-api/signals", query);
}

export function getTimeline(query: Query = {}) {
  return publicFetch<{ items?: SignalItem[]; groups?: Array<{ label?: string; items?: SignalItem[] }>; count?: number }>(
    "/public-api/signal-timeline",
    query
  );
}

export function getCoinSearch(query: Query = {}) {
  return publicFetch<{ items?: Array<{ symbol?: string; label?: string; count?: number; subtitle?: string }> }>("/public-api/coin-search", query);
}

export function getDecisionStats(windowSec = 86400) {
  return publicFetch<Record<string, unknown>>("/public-api/decisions/stats", { window_sec: windowSec });
}

export function getDecisions(query: Query = {}) {
  return publicFetch<{ items?: DecisionItem[]; decisions?: DecisionItem[]; summary?: Record<string, unknown> }>("/public-api/decisions", query);
}

export function getDecision(symbol: string) {
  return publicFetch<DecisionItem>("/public-api/decision", { symbol });
}

export function getOutcomes(query: Query = {}) {
  return publicFetch<{ items?: OutcomeItem[]; count?: number }>("/public-api/outcomes", query);
}

export function getOutcomeStats(horizon = "1h") {
  return publicFetch<Record<string, unknown>>("/public-api/outcomes/stats", { horizon });
}

export function getSymbolOutcomes(symbol: string, query: Query = {}) {
  return publicFetch<{ items?: OutcomeItem[]; count?: number }>("/public-api/symbol-outcomes", { symbol, ...query });
}

export function getCoinDetail(symbol: string) {
  return publicFetch<Record<string, unknown>>("/public-api/coin-detail", { symbol });
}

export function getSymbolTimeline(symbol: string) {
  return publicFetch<{ items?: SignalItem[]; groups?: Array<{ label?: string; items?: SignalItem[] }> }>("/public-api/signal-timeline", { symbol, limit: 80 });
}

export function getBacktestDecision(query: Query = {}) {
  return publicFetch<BacktestPayload>("/public-api/backtest/decision", query);
}

export function getBacktestMatrix(query: Query = {}) {
  return publicFetch<BacktestMatrixPayload>("/public-api/backtest/decision/matrix", query);
}

export function getBacktestDetail(query: Query = {}) {
  return publicFetch<{ items?: OutcomeItem[]; count?: number }>("/public-api/backtest/decision/detail", query);
}
