"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { navItems } from "@/lib/routes";

type NavIcon = (typeof navItems)[number]["icon"];

function Icon({ name, className = "" }: { name: NavIcon | "sun" | "moon" | "globe"; className?: string }) {
  const common = { fill: "none", stroke: "currentColor", strokeLinecap: "round" as const, strokeLinejoin: "round" as const, strokeWidth: 1.7 };
  const paths: Record<string, React.ReactNode> = {
    radar: <><circle {...common} cx="12" cy="12" r="8"/><path {...common} d="M12 4v8l5.4 3.1M7.8 8.1 12 12"/><circle cx="12" cy="12" fill="currentColor" r="1.4"/></>,
    info: <path {...common} d="M5 5.5h14v13H5zM8 9h8M8 12h8M8 15h5"/>,
    funds: <path {...common} d="M4 18.5h16M6.5 16V11M12 16V6M17.5 16V9"/>,
    spark: <><path {...common} d="m12 3 1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5L12 3Z"/><path {...common} d="m18.5 16 .6 2.1 2 .6-2 .6-.6 2.1-.6-2.1-2-.6 2-.6.6-2.1Z"/></>,
    watchlist: <path {...common} d="m12 4 2.3 4.7 5.2.8-3.8 3.7.9 5.2-4.6-2.5-4.6 2.5.9-5.2-3.8-3.7 5.2-.8L12 4Z"/>,
    sun: <><circle {...common} cx="12" cy="12" r="3.3"/><path {...common} d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></>,
    moon: <path {...common} d="M20 15.2A8 8 0 0 1 8.8 4 8.2 8.2 0 1 0 20 15.2Z"/>,
    globe: <><circle {...common} cx="12" cy="12" r="9"/><path {...common} d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></>
  };
  return <svg aria-hidden="true" className={className} viewBox="0 0 24 24">{paths[name]}</svg>;
}

function BrandMark({ workstation = false }: { workstation?: boolean }) {
  return (
    <span className={`grid shrink-0 place-items-center overflow-hidden bg-[#111318] text-white ${workstation ? "h-[38px] w-[38px] rounded-[7px]" : "h-[26px] w-[26px] rounded-[5px]"}`}>
      <svg aria-hidden="true" className={workstation ? "h-7 w-7" : "h-5 w-5"} viewBox="0 0 28 28">
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
  const workstation = ["/radar", "/info", "/funds"].some((path) => pathname === path || pathname.startsWith(`${path}/`));
  const visibleNavItems = workstation ? navItems.filter((item) => item.href !== "/watchlist") : navItems;
  const [health, setHealth] = useState<"checking" | "live" | "degraded" | "offline">("checking");
  const [clock, setClock] = useState("");
  const [theme, setTheme] = useState<"light" | "dark">("light");

  useEffect(() => {
    const nextTheme = window.localStorage.getItem("paoxx-workstation-theme") === "dark" ? "dark" : "light";
    setTheme(nextTheme);
    document.documentElement.dataset.theme = nextTheme;
    document.documentElement.style.colorScheme = nextTheme;
  }, []);

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

  const toggleTheme = () => {
    const nextTheme = theme === "light" ? "dark" : "light";
    setTheme(nextTheme);
    window.localStorage.setItem("paoxx-workstation-theme", nextTheme);
    document.documentElement.dataset.theme = nextTheme;
    document.documentElement.style.colorScheme = nextTheme;
  };
  const isActive = (href: string) => pathname === href || pathname.startsWith(`${href}/`) || (href === "/radar" && pathname === "/");
  const healthLabel = health === "live" ? "LIVE" : health === "degraded" ? "DEGRADED" : health === "offline" ? "OFFLINE" : "CHECK";

  return (
    <>
      <header className={`sticky top-0 z-30 border-b border-border-subtle bg-surface-canvas/95 backdrop-blur ${workstation ? "workstation-header" : ""}`}>
        <div className={`mx-auto flex items-center ${workstation ? "h-[69px] max-w-none gap-[21px] px-[22px]" : "h-[44px] gap-3 px-3.5 max-w-[1280px]"}`}>
          <Link aria-label="Paoxx 雷达" className={`flex shrink-0 items-center ${workstation ? "h-full w-[143px] gap-3 border-r border-border-subtle min-[1600px]:w-[146px]" : "gap-2"}`} href="/radar"><BrandMark workstation={workstation}/><span className={`${workstation ? "text-[16px]" : "text-[13px]"} font-bold tracking-[.12em] text-text-primary`}>PAOXX</span></Link>
          <nav aria-label="主导航" className={`hidden min-w-0 items-stretch md:flex ${workstation ? "h-[44px] flex-none rounded-[4px] border border-border-subtle bg-[#e9edf3] p-px" : "h-full flex-1"}`}>
            {visibleNavItems.map((item) => {
              const active = isActive(item.href);
              const workstationWidth = item.href === "/radar" ? "w-[70px] min-[1600px]:w-[72px]" : item.href === "/agents" ? "w-[130px] min-[1600px]:w-[132px]" : "w-[78px] min-[1600px]:w-[80px]";
              return <Link aria-current={active ? "page" : undefined} className={`group relative flex items-center justify-center gap-1 px-2 font-semibold transition-colors ${workstation ? `${workstationWidth} text-[13px] ${active ? "rounded-[3px] bg-surface-panel text-good ring-1 ring-good/25" : "text-text-secondary hover:bg-surface-panel hover:text-text-primary"}` : `min-w-[58px] text-[10px] ${active ? "bg-[#edf7f3] text-good" : "text-text-secondary hover:bg-surface-low hover:text-text-primary"}`}`} href={item.href} key={item.href}>
                {!workstation ? <Icon className="h-[15px] w-[15px]" name={item.icon}/> : null}<span>{item.label}</span>
                {"badge" in item ? <span className="absolute right-0.5 top-1 rounded-full bg-[#ef4444] px-1 py-px text-[6px] font-bold leading-none text-white">{item.badge}</span> : null}
                {active && !workstation ? <span className="absolute inset-x-2 bottom-0 h-[2px] rounded-t bg-good"/> : null}
              </Link>;
            })}
          </nav>
          <div className={`ml-auto flex h-full items-center ${workstation ? "gap-[17px] min-[1600px]:mr-[5px]" : "gap-1.5"}`}>
            <span className={`hidden items-center gap-1 rounded-full bg-good/10 font-mono font-bold tracking-wide text-good sm:inline-flex ${workstation ? "h-[30px] px-4 text-[12px]" : "px-2 py-1 text-[8px]"}`}><span className={`${workstation ? "h-2 w-2" : "h-1.5 w-1.5"} rounded-full ${health === "live" ? "animate-pulse bg-good" : health === "offline" ? "bg-risk" : "bg-warn"}`}/>{healthLabel}</span>
            <span className={`hidden text-center font-mono tabular-nums text-text-muted lg:inline ${workstation ? "min-w-[143px] text-[12px]" : "min-w-[94px] text-[8px]"}`}>{clock || "--:--:--"}&nbsp; UTC+8</span>
            <button aria-label={`切换到${theme === "light" ? "深色" : "浅色"}主题`} className={`grid place-items-center rounded-[4px] border border-border-subtle text-text-secondary hover:bg-surface-low ${workstation ? "h-[38px] w-[38px] bg-[#eef1f5]" : "h-7 w-7"}`} onClick={toggleTheme} type="button"><Icon className={workstation ? "h-[18px] w-[18px]" : "h-[14px] w-[14px]"} name={theme === "light" ? "moon" : "sun"}/></button>
            {workstation ? <Link aria-label="打开自选" className="hidden h-[38px] items-center gap-1.5 rounded-[4px] border border-border-subtle bg-[#eef1f5] px-4 text-[13px] font-medium text-text-secondary hover:bg-surface-low lg:flex" href="/watchlist"><Icon className="h-[18px] w-[18px]" name="watchlist"/><span>自选</span></Link> : null}
            <button aria-label="当前语言：中文" className={`hidden items-center gap-1.5 rounded-[4px] font-medium text-text-secondary hover:bg-surface-low sm:flex ${workstation ? "h-[38px] px-3 text-[12px]" : "h-7 px-2 text-[9px]"}`} type="button"><Icon className={workstation ? "h-[18px] w-[18px]" : "h-3.5 w-3.5"} name="globe"/><span>中文</span></button>
            {workstation ? <span aria-hidden="true" className="hidden w-[38px] shrink-0 md:block min-[1600px]:w-[48px]"><span className="fixed right-[26px] top-[17px] grid h-[36px] w-[36px] place-items-center rounded-full bg-[#f59e0b] text-[15px] font-semibold text-white min-[1600px]:right-[28px]">P</span></span> : null}
          </div>
        </div>
      </header>
      <nav aria-label="移动端导航" className={`fixed inset-x-0 bottom-0 z-30 grid border-t border-border-subtle bg-surface-canvas/95 pb-[max(.3rem,env(safe-area-inset-bottom))] pt-1 backdrop-blur md:hidden ${workstation ? "grid-cols-4" : "grid-cols-5"}`}>
        {visibleNavItems.map((item) => {
          const active = isActive(item.href);
          return <Link aria-current={active ? "page" : undefined} className={`relative flex min-h-[56px] flex-col items-center justify-center gap-1 text-[10px] font-semibold ${active ? "text-primary-600" : "text-text-muted"}`} href={item.href} key={item.href}><Icon className="h-[18px] w-[18px]" name={item.icon}/><span>{item.label}</span>{active ? <span className="absolute left-1/2 top-0 h-0.5 w-5 -translate-x-1/2 bg-primary-500"/> : null}</Link>;
        })}
      </nav>
    </>
  );
}
