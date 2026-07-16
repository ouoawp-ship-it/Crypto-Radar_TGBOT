export const WATCHLIST_STORAGE_KEY = "paoxx.public.watchlist.v1";
export const WATCHLIST_CHANGE_EVENT = "paoxx-watchlist-change";
export const WATCHLIST_LIMIT = 12;

export function normalizeWatchSymbol(value: string): string {
  const clean = String(value || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (!clean || clean.length > 24) return "";
  const symbol = clean.endsWith("USDT") ? clean : `${clean}USDT`;
  return /^[A-Z0-9]{2,20}USDT$/.test(symbol) ? symbol : "";
}

export function loadWatchlist(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const value = JSON.parse(window.localStorage.getItem(WATCHLIST_STORAGE_KEY) || "[]");
    if (!Array.isArray(value)) return [];
    return Array.from(new Set(value.map((item) => normalizeWatchSymbol(String(item))).filter(Boolean))).slice(0, WATCHLIST_LIMIT);
  } catch {
    return [];
  }
}

export function saveWatchlist(symbols: string[]): string[] {
  const next = Array.from(new Set(symbols.map(normalizeWatchSymbol).filter(Boolean))).slice(0, WATCHLIST_LIMIT);
  if (typeof window !== "undefined") {
    window.localStorage.setItem(WATCHLIST_STORAGE_KEY, JSON.stringify(next));
    window.dispatchEvent(new CustomEvent(WATCHLIST_CHANGE_EVENT, { detail: next }));
  }
  return next;
}

export function toggleWatchSymbol(symbol: string): string[] {
  const target = normalizeWatchSymbol(symbol);
  if (!target) return loadWatchlist();
  const current = loadWatchlist();
  return saveWatchlist(current.includes(target) ? current.filter((item) => item !== target) : [...current, target]);
}
