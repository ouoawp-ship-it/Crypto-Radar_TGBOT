"use client";

import { FormEvent, useEffect, useState } from "react";
import { ErrorState } from "./ErrorState";
import { PageTitle } from "./PageTitle";
import { SignalDetailDrawer } from "./SignalDetailDrawer";
import { getSignals } from "@/lib/api";
import { formatDateTime, safeText } from "@/lib/format";
import type { SignalItem } from "@/lib/types";

export function LegacySignalRadar() {
  const [items, setItems] = useState<SignalItem[]>([]);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<number | string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  async function load() {
    setLoading(true);
    setError("");
    try {
      const payload = await getSignals({ limit: 60, status: "sent", symbol: query.toUpperCase() });
      setItems(payload.items || []);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "兼容信号加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, [query]);
  function submit(event: FormEvent) { event.preventDefault(); setQuery(draft.trim()); }

  return (
    <div className="space-y-3">
      <PageTitle title="信号雷达 · 兼容模式" subtitle="V2 聚合已停用；这里继续读取稳定的公开信号接口，不影响 Telegram 推送。" tags={["ROLLBACK MODE", "signals API"]} />
      <form className="cockpit-panel flex flex-col gap-2 p-3 sm:flex-row" onSubmit={submit}><input aria-label="兼容模式币种筛选" className="input flex-1" placeholder="BTC 或 BTCUSDT" value={draft} onChange={(event) => setDraft(event.target.value.toUpperCase())} /><button className="btn" type="submit">筛选</button><button className="btn-secondary" onClick={() => void load()} type="button">刷新</button></form>
      {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}
      <section className="cockpit-panel divide-y divide-border-subtle">
        {loading ? <div className="h-48 animate-pulse bg-surface-container-low" /> : items.map((item) => <button className="w-full p-4 text-left hover:bg-surface-container-low" key={String(item.public_ref || item.id)} onClick={() => setSelected(item.public_ref || item.id || "")} type="button"><div className="flex items-center justify-between gap-3"><span className="table-number font-semibold text-text-primary">{safeText(item.symbol, "GLOBAL")}</span><span className="text-[10px] text-text-muted">{formatDateTime(item.time)}</span></div><p className="mt-2 text-xs leading-5 text-text-secondary">{safeText(item.display?.summary || item.excerpt)}</p></button>)}
        {!loading && !items.length ? <div className="px-4 py-16 text-center text-sm text-text-muted">当前没有匹配的已发送信号。</div> : null}
      </section>
      <SignalDetailDrawer onClose={() => setSelected("")} signalId={selected} />
    </div>
  );
}
