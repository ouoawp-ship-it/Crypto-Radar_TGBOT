import Link from "next/link";
import { navItems } from "@/lib/routes";

export function Header() {
  return (
    <header className="sticky top-0 z-30 border-b border-border-subtle bg-white/95 backdrop-blur">
      <div className="mx-auto flex h-16 max-w-[1440px] items-center justify-between gap-4 px-4 sm:px-6">
        <Link href="/" className="flex min-w-0 items-center gap-3">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-primary-700 text-sm font-semibold text-white">
            CR
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-text-primary">泡泡抓币 Crypto Radar</div>
            <div className="truncate text-xs text-text-muted">信号、决策、结果与模型健康看板</div>
          </div>
        </Link>
        <div className="flex shrink-0 items-center gap-2">
          <Link className="btn-secondary hidden sm:inline-flex" href="/api-docs">
            公开 API
          </Link>
          <a className="btn" href="/admin">
            后台控制台
          </a>
        </div>
      </div>
      <nav className="mx-auto flex max-w-[1440px] gap-2 overflow-x-auto px-4 pb-3 text-sm font-semibold lg:hidden">
        {navItems.map((item) => (
          <Link className="whitespace-nowrap rounded-lg border border-border-subtle bg-white px-3 py-2 text-text-secondary" href={item.href} key={item.href}>
            {item.label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
