import Link from "next/link";
import { navItems } from "@/lib/routes";

export function Header() {
  return (
    <header className="sticky top-0 z-30 border-b border-white/10 bg-surface-950/85 backdrop-blur-xl">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-4 sm:px-6">
        <Link href="/" className="flex min-w-0 items-center gap-3">
          <div className="grid h-10 w-10 shrink-0 place-items-center rounded-2xl border border-cyanline/40 bg-cyanline/10 text-lg font-black text-cyan-200">
            P
          </div>
          <div className="min-w-0">
            <div className="truncate text-base font-black tracking-wide text-white">Paoxx 信号雷达</div>
            <div className="truncate text-xs text-slate-400">加密市场信号、决策、结果追踪与回测仪表盘</div>
          </div>
        </Link>
        <div className="flex shrink-0 items-center gap-2">
          <Link className="hidden rounded-xl border border-white/10 px-3 py-2 text-xs font-bold text-slate-300 hover:bg-white/5 sm:inline-flex" href="/api-docs">
            公开 API
          </Link>
          <a className="rounded-xl border border-cyanline/40 bg-cyanline/10 px-3 py-2 text-xs font-bold text-cyan-100 hover:bg-cyanline/20" href="/admin">
            后台控制台
          </a>
        </div>
      </div>
      <nav className="mx-auto flex max-w-7xl gap-2 overflow-x-auto px-4 pb-3 text-sm font-bold lg:hidden">
        {navItems.map((item) => (
          <Link className="whitespace-nowrap rounded-xl border border-white/10 px-3 py-2 text-slate-300 hover:bg-white/5" href={item.href} key={item.href}>
            {item.label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
