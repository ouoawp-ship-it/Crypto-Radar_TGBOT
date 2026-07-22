"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { navItems } from "@/lib/routes";

type NavIcon = (typeof navItems)[number]["icon"];

function Icon({ name, className = "" }: { name: NavIcon; className?: string }) {
  const common = { fill: "none", stroke: "currentColor", strokeLinecap: "round" as const, strokeLinejoin: "round" as const, strokeWidth: 1.7 };
  const paths: Record<NavIcon, React.ReactNode> = {
    radar: <><circle {...common} cx="12" cy="12" r="8"/><path {...common} d="M12 4v8l5.4 3.1M7.8 8.1 12 12"/><circle cx="12" cy="12" fill="currentColor" r="1.4"/></>,
    info: <path {...common} d="M5 5.5h14v13H5zM8 9h8M8 12h8M8 15h5"/>,
    funds: <path {...common} d="M4 18.5h16M6.5 16V11M12 16V6M17.5 16V9"/>,
  };
  return <svg aria-hidden="true" className={className} viewBox="0 0 24 24">{paths[name]}</svg>;
}

function BrandMark() {
  return (
    <span className="grid h-8 w-8 shrink-0 place-items-center rounded-md border border-white/10 bg-white/[0.04] text-white">
      <svg aria-hidden="true" className="h-5 w-5" viewBox="0 0 28 28">
        <path d="M5.5 18.8c3.3 0 4-8.1 7.7-8.1 3 0 3.8 5.5 6.4 5.5 1.2 0 2.1-.8 3-2.3" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="2.2"/>
        <circle cx="6" cy="18.7" fill="currentColor" r="2"/><circle cx="13.2" cy="10.7" fill="currentColor" r="2"/><circle cx="20" cy="16.1" fill="currentColor" r="2"/>
      </svg>
    </span>
  );
}

function formatUtc8(now: Date) {
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(now);
}

export function Header() {
  const pathname = usePathname();
  const [health, setHealth] = useState<"checking" | "live" | "degraded" | "offline">("checking");
  const [clock, setClock] = useState("");

  useEffect(() => {
    const update = () => setClock(formatUtc8(new Date()));
    update();
    const timer = window.setInterval(update, 1_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    let disposed = false;
    let activeController: AbortController | null = null;
    async function checkHealth() {
      activeController?.abort();
      const controller = new AbortController();
      activeController = controller;
      const timer = window.setTimeout(() => controller.abort(), 12_000);
      try {
        const response = await fetch("/public-api/health", { cache: "no-store", signal: controller.signal });
        const payload = await response.json() as { ok?: boolean; data?: { status?: string } };
        if (!disposed) setHealth(!response.ok || payload.ok === false ? "offline" : payload.data?.status === "ok" ? "live" : "degraded");
      } catch {
        if (!disposed && activeController === controller) setHealth(window.navigator.onLine ? "degraded" : "offline");
      } finally {
        window.clearTimeout(timer);
      }
    }
    const handleVisibility = () => { if (document.visibilityState === "visible") void checkHealth(); };
    void checkHealth();
    const interval = window.setInterval(() => void checkHealth(), 60_000);
    document.addEventListener("visibilitychange", handleVisibility);
    window.addEventListener("online", checkHealth);
    return () => {
      disposed = true;
      activeController?.abort();
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibility);
      window.removeEventListener("online", checkHealth);
    };
  }, []);

  const isActive = (href: string) => pathname === href || pathname.startsWith(`${href}/`) || (href === "/radar" && pathname === "/");
  const healthLabel = health === "live" ? "LIVE" : health === "degraded" ? "DEGRADED" : health === "offline" ? "OFFLINE" : "CHECK";

  return (
    <>
      <header className="workstation-header sticky top-0 z-30 border-b border-border-subtle bg-surface-canvas/95 backdrop-blur-xl">
        <div className="mx-auto flex h-14 w-full items-center gap-4 px-3 sm:px-5">
          <Link aria-label="Paoxx 雷达" className="flex shrink-0 items-center gap-2.5" href="/radar">
            <BrandMark/>
            <span className="font-semibold tracking-[.14em] text-text-primary">PAOXX</span>
            <span className="hidden border-l border-border-subtle pl-2 text-[9px] font-medium tracking-[.12em] text-text-muted xl:inline">MARKET INTELLIGENCE</span>
          </Link>
          <nav aria-label="主导航" className="hidden h-9 items-center gap-1 rounded-md border border-border-subtle bg-surface-low p-1 md:flex">
            {navItems.map((item) => {
              const active = isActive(item.href);
              return <Link aria-current={active ? "page" : undefined} className={`relative flex h-7 min-w-[72px] items-center justify-center gap-1.5 rounded px-3 text-xs font-semibold transition-colors ${active ? "bg-surface-container text-text-primary" : "text-text-muted hover:bg-surface-container-low hover:text-text-secondary"}`} href={item.href} key={item.href}><Icon className="h-4 w-4" name={item.icon}/><span>{item.label}</span>{active ? <span className="absolute inset-x-3 -bottom-[5px] h-px bg-good"/> : null}</Link>;
            })}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <span className="hidden font-mono text-[10px] tabular-nums text-text-muted lg:inline">{clock || "--:--:--"} UTC+8</span>
            <span className="inline-flex h-7 items-center gap-1.5 rounded-full border border-border-subtle bg-surface-low px-2.5 font-mono text-[9px] font-semibold tracking-wide text-text-secondary"><span className={`h-1.5 w-1.5 rounded-full ${health === "live" ? "animate-pulse bg-good" : health === "offline" ? "bg-risk" : "bg-warn"}`}/>{healthLabel}</span>
          </div>
        </div>
      </header>
      <nav aria-label="移动端导航" className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-3 border-t border-border-subtle bg-surface-canvas/95 pb-[max(.3rem,env(safe-area-inset-bottom))] pt-1 backdrop-blur-xl md:hidden">
        {navItems.map((item) => {
          const active = isActive(item.href);
          return <Link aria-current={active ? "page" : undefined} className={`relative flex min-h-[56px] flex-col items-center justify-center gap-1 text-[10px] font-semibold ${active ? "text-text-primary" : "text-text-muted"}`} href={item.href} key={item.href}><Icon className="h-[18px] w-[18px]" name={item.icon}/><span>{item.label}</span>{active ? <span className="absolute left-1/2 top-0 h-0.5 w-5 -translate-x-1/2 bg-good"/> : null}</Link>;
        })}
      </nav>
    </>
  );
}
