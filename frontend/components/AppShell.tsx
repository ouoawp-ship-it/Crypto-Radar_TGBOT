import Link from "next/link";
import { Header } from "./Header";
import { Sidebar } from "./Sidebar";

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen">
      <Header />
      <div className="mx-auto flex w-full max-w-7xl gap-5 px-4 pb-8 pt-4 sm:px-6">
        <Sidebar />
        <main className="min-w-0 flex-1">{children}</main>
      </div>
      <footer className="border-t border-white/10 px-4 py-6 text-center text-xs text-slate-500">
        <span>仅用于信号整理、结果复盘和风险提示，不构成投资建议，不执行自动交易。</span>
        <span className="mx-2">·</span>
        <Link className="text-cyan-300" href="/api-docs">
          公开 API
        </Link>
      </footer>
    </div>
  );
}
