"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { navItems } from "@/lib/routes";
import { cockpitV2Preview } from "@/lib/features";
import { ThemeToggle } from "./ThemeToggle";

export function Header() {
  const pathname = usePathname();
  const [health, setHealth] = useState<"checking" | "live" | "degraded" | "offline">("checking");

  useEffect(() => {
    let disposed = false;
    let activeController: AbortController | null = null;

    async function checkHealth() {
      activeController?.abort();
      const controller = new AbortController();
      activeController = controller;
      const timer = window.setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch("/public-api/health", { cache: "no-store", signal: controller.signal });
        const payload = await response.json() as { ok?: boolean; data?: { status?: string } };
        if (!disposed) {
          if (!response.ok || payload.ok === false) setHealth("offline");
          else setHealth(payload.data?.status === "ok" ? "live" : "degraded");
        }
      } catch {
        if (!disposed && activeController === controller) setHealth("offline");
      } finally {
        window.clearTimeout(timer);
      }
    }

    const handleVisibility = () => {
      if (document.visibilityState === "visible") void checkHealth();
    };
    void checkHealth();
    const interval = window.setInterval(() => void checkHealth(), 60_000);
    window.addEventListener("online", checkHealth);
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      disposed = true;
      activeController?.abort();
      window.clearInterval(interval);
      window.removeEventListener("online", checkHealth);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, []);

  const healthMeta = health === "live"
    ? { label: "LIVE", detail: "公开 API 正常", dot: "animate-pulse bg-good" }
    : health === "degraded"
      ? { label: "DEGRADED", detail: "公开 API 可用，部分数据正在积累或降级", dot: "bg-warn" }
    : health === "offline"
      ? { label: "OFFLINE", detail: "公开 API 暂不可用", dot: "bg-risk" }
      : { label: "CHECK", detail: "正在检查公开 API", dot: "animate-pulse bg-warn" };
  return (
    <header className="sticky top-0 z-30 border-b border-border-subtle bg-surface-panel/95 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-[1920px] items-center justify-between gap-3 px-3 sm:px-4 lg:px-5">
        <Link href="/" className="flex min-w-0 items-center gap-3">
          <div className="grid h-8 w-8 shrink-0 place-items-center rounded-md bg-primary-700 text-xs font-semibold text-on-primary">
            PP
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold tracking-tight text-text-primary">泡泡雷达</div>
            <div className="hidden truncate text-[10px] text-text-muted sm:block">MARKET INTELLIGENCE{cockpitV2Preview ? " · PREVIEW" : ""}</div>
          </div>
        </Link>

        <nav aria-label="主要导航" className="hidden h-full items-center gap-1 md:flex">
          {navItems.map((item) => {
            const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            return (
              <Link
                aria-current={active ? "page" : undefined}
                className={`relative flex h-full items-center px-3 text-sm font-medium transition ${active ? "text-primary-700" : "text-text-secondary hover:text-text-primary"}`}
                href={item.href}
                key={item.href}
              >
                {item.label}
                {active ? <span className="absolute inset-x-3 bottom-0 h-0.5 rounded-full bg-primary-600" /> : null}
              </Link>
            );
          })}
        </nav>

        <div className="flex shrink-0 items-center gap-2">
          <span aria-label={healthMeta.detail} className="hidden items-center gap-1.5 rounded-full border border-border-subtle px-2.5 py-1 text-[11px] font-semibold text-text-secondary lg:inline-flex">
            <span className={`h-1.5 w-1.5 rounded-full ${healthMeta.dot}`} />{healthMeta.label}
          </span>
          <ThemeToggle />
          <a className="hidden h-9 items-center rounded-md border border-border-subtle bg-surface-panel px-3 text-xs font-semibold text-text-secondary transition hover:border-primary-100 hover:text-primary-700 sm:inline-flex" href="/admin">
            控制台
          </a>
        </div>
      </div>
      <nav aria-label="移动导航" className="scrollbar-none mx-auto flex max-w-[1920px] gap-1 overflow-x-auto border-t border-border-subtle px-3 py-2 text-xs font-medium md:hidden">
        {navItems.map((item) => (
          <Link className={`whitespace-nowrap rounded-md px-3 py-1.5 ${pathname === item.href || (item.href !== "/" && pathname.startsWith(item.href)) ? "bg-primary-50 text-primary-700" : "text-text-secondary"}`} href={item.href} key={item.href}>
            {item.label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
