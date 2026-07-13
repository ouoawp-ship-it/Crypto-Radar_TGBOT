"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { navItems } from "@/lib/routes";

export function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="hidden w-64 shrink-0 lg:block">
      <nav className="panel sticky top-20 p-3">
        <div className="mb-3 px-3 text-xs font-medium tracking-[0.08em] text-text-muted">公开前台</div>
        <div className="space-y-1">
          {navItems.map((item) => {
            const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`flex rounded-lg px-3 py-2 text-sm font-medium transition ${
                  active ? "bg-primary-50 text-primary-700" : "text-text-secondary hover:bg-surface-canvas"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </div>
      </nav>
    </aside>
  );
}
