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
      <span aria-hidden="true">{active ? "★" : "☆"}</span>
      {compact ? (active ? "已自选" : "自选") : (active ? "已加入自选" : "加入自选")}
    </button>
  );
}
