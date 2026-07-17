"use client";

import { useEffect, useState } from "react";
import { loadWatchlist, normalizeWatchSymbol, toggleWatchSymbol, WATCHLIST_CHANGE_EVENT } from "@/lib/watchlist";

export function WatchlistButton({ symbol, compact = false }: { symbol: string; compact?: boolean }) {
  const normalized = normalizeWatchSymbol(symbol);
  const [active, setActive] = useState(false);

  useEffect(() => {
    const sync = () => setActive(Boolean(normalized && loadWatchlist().includes(normalized)));
    sync();
    window.addEventListener("storage", sync);
    window.addEventListener(WATCHLIST_CHANGE_EVENT, sync);
    return () => {
      window.removeEventListener("storage", sync);
      window.removeEventListener(WATCHLIST_CHANGE_EVENT, sync);
    };
  }, [normalized]);

  return (
    <button
      aria-pressed={active}
      className={compact ? "btn-secondary px-3" : "btn-secondary"}
      disabled={!normalized}
      onClick={() => setActive(toggleWatchSymbol(normalized).includes(normalized))}
      type="button"
    >
      <svg aria-hidden="true" className={`h-4 w-4 ${active ? "fill-primary-500 text-primary-500" : "fill-none text-text-muted"}`} viewBox="0 0 24 24">
        <path d="m12 3.8 2.5 5 5.5.8-4 3.9.9 5.5-4.9-2.6L7.1 19l.9-5.5-4-3.9 5.5-.8 2.5-5Z" stroke="currentColor" strokeLinejoin="round" strokeWidth="1.6" />
      </svg>
      {compact ? (active ? "已自选" : "自选") : (active ? "已加入自选" : "加入自选")}
    </button>
  );
}
