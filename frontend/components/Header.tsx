"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { navItems } from "@/lib/routes";

type NavIcon = (typeof navItems)[number]["icon"];

function NavGlyph({ icon }: { icon: NavIcon }) {
  const common = { fill: "none", stroke: "currentColor", strokeLinecap: "round" as const, strokeLinejoin: "round" as const, strokeWidth: 1.7 };
  if (icon === "radar") return <svg aria-hidden="true" viewBox="0 0 24 24"><circle {...common} cx="12" cy="12" r="8"/><path {...common} d="M12 4v8l5.5 3.2M8.2 8.2l3.8 3.8"/><circle cx="12" cy="12" fill="currentColor" r="1.5"/></svg>;
  if (icon === "info") return <svg aria-hidden="true" viewBox="0 0 24 24"><path {...common} d="M5 5.5h14v13H5zM8 9h8M8 12h8M8 15h5"/></svg>;
  if (icon === "funds") return <svg aria-hidden="true" viewBox="0 0 24 24"><path {...common} d="M4 18.5h16M6.5 16V11M12 16V6M17.5 16V9"/></svg>;
  if (icon === "spark") return <svg aria-hidden="true" viewBox="0 0 24 24"><path {...common} d="m12 3 1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5L12 3Z"/><path {...common} d="m18.5 16 .6 2.1 2 .6-2 .6-.6 2.1-.6-2.1-2-.6 2-.6.6-2.1Z"/></svg>;
  return <svg aria-hidden="true" viewBox="0 0 24 24"><path {...common} d="m12 4 2.3 4.7 5.2.8-3.8 3.7.9 5.2-4.6-2.5-4.6 2.5.9-5.2-3.8-3.7 5.2-.8L12 4Z"/></svg>;
}

function BrandMark() {
  return (
    <span className="grid h-9 w-9 shrink-0 place-items-center rounded-md border border-primary-500/45 bg-surface-low text-primary-500 shadow-[0_0_18px_rgba(0,210,239,0.12)]">
      <svg aria-hidden="true" className="h-5 w-5" viewBox="0 0 24 24"><path d="M4 15.5c3.7 0 4.5-7 8-7 2.5 0 3 3.8 5.1 3.8 1.1 0 1.9-.8 2.9-2.3" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="2"/><circle cx="5" cy="15.5" fill="currentColor" r="2"/><circle cx="12" cy="8.5" fill="currentColor" r="2"/><circle cx="18" cy="12.2" fill="currentColor" r="2"/></svg>
    </span>
  );
}

export function Header() {
  const pathname = usePathname();
  const workstation = ["/radar", "/info", "/funds"].some((path) => pathname === path || pathname.startsWith(`${path}/`));
  const visibleNavItems = workstation ? navItems.filter((item) => item.href !== "/watchlist") : navItems;
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
        if (!disposed) setHealth(!response.ok || payload.ok === false ? "offline" : payload.data?.status === "ok" ? "live" : "degraded");
      } catch {
        if (!disposed && activeController === controller) setHealth("offline");
      } finally {
        window.clearTimeout(timer);
      }
    }
    const handleVisibility = () => { if (document.visibilityState === "visible") void checkHealth(); };
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
    ? { label: "LIVE", detail: "公开 API 正常", dot: "bg-good" }
    : health === "degraded"
      ? { label: "DEGRADED", detail: "公开 API 可用，部分数据正在积累或降级", dot: "bg-warn" }
      : health === "offline"
        ? { label: "OFFLINE", detail: "公开 API 暂不可用", dot: "bg-risk" }
        : { label: "CHECK", detail: "正在检查公开 API", dot: "animate-pulse bg-warn" };
  const isActive = (href: string) => pathname === href || pathname.startsWith(`${href}/`) || (href === "/radar" && pathname === "/");

  return (
    <>
      <header className={`sticky top-0 z-30 border-b border-border-subtle bg-surface-canvas/95 backdrop-blur-md ${workstation ? "workstation-header" : ""}`}>
        <div className={`mx-auto flex h-[55px] items-center gap-5 px-3 ${workstation ? "max-w-none lg:px-4" : "max-w-[1280px] sm:px-5 lg:px-6"}`}>
          <Link aria-label="Paoxx 市场总览" className="flex shrink-0 items-center gap-2.5" href="/">
            <BrandMark />
            <span className="text-[18px] font-bold tracking-[-0.025em] text-text-primary">Paoxx</span>
          </Link>

          <nav aria-label="主要导航" className="hidden h-full min-w-0 flex-1 items-center justify-center md:flex">
            {visibleNavItems.map((item) => {
              const active = isActive(item.href);
              return (
                <Link aria-current={active ? "page" : undefined} className={`group relative flex h-full items-center gap-2 px-3.5 text-[13px] font-semibold transition-colors ${active ? "text-text-primary" : "text-text-muted hover:text-text-secondary"}`} href={item.href} key={item.href}>
                  <span className={`h-4 w-4 ${active ? "text-primary-500" : "text-text-muted group-hover:text-text-secondary"}`}><NavGlyph icon={item.icon} /></span>
                  <span>{item.label}</span>
                  {"badge" in item ? <span className="rounded-sm border border-primary-500/25 bg-primary-500/10 px-1 py-0.5 text-[9px] font-bold text-primary-500">{item.badge}</span> : null}
                  {active ? <span className="absolute inset-x-3 bottom-0 h-px bg-primary-500" /> : null}
                </Link>
              );
            })}
          </nav>

          <div className="ml-auto flex items-center gap-2">
            <span aria-label={healthMeta.detail} className="inline-flex h-8 items-center gap-2 rounded-md border border-border-subtle bg-surface-low px-2.5 font-mono text-[10px] font-semibold tracking-wide text-text-secondary">
              <span className={`h-1.5 w-1.5 rounded-full ${healthMeta.dot}`} />{healthMeta.label}
            </span>
          </div>
        </div>
      </header>

      <nav aria-label="移动导航" className={`fixed inset-x-0 bottom-0 z-30 grid border-t border-border-subtle bg-surface-canvas/95 pb-[max(0.35rem,env(safe-area-inset-bottom))] pt-1 backdrop-blur-md md:hidden ${workstation ? "grid-cols-4" : "grid-cols-5"}`}>
        {visibleNavItems.map((item) => {
          const active = isActive(item.href);
          return (
            <Link aria-current={active ? "page" : undefined} className={`relative flex min-h-[58px] flex-col items-center justify-center gap-1 text-[10px] font-semibold transition-colors ${active ? "text-primary-500" : "text-text-muted"}`} href={item.href} key={item.href}>
              <span className="h-[18px] w-[18px]"><NavGlyph icon={item.icon} /></span>
              <span>{item.label}</span>
              {active ? <span className="absolute left-1/2 top-0 h-0.5 w-5 -translate-x-1/2 bg-primary-500" /> : null}
            </Link>
          );
        })}
      </nav>
    </>
  );
}
