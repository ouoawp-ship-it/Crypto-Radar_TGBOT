import Link from "next/link";
import { Header } from "./Header";
import { Sidebar } from "./Sidebar";

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-surface-canvas">
      <Header />
      <div className="mx-auto flex w-full max-w-[1440px] gap-5 px-4 pb-8 pt-5 sm:px-6">
        <Sidebar />
        <main className="min-w-0 flex-1">{children}</main>
      </div>
      <footer className="border-t border-border-subtle bg-white px-4 py-5 text-center text-xs text-text-muted">
        <span>仅用于信号整理、结果复盘和风险提示，不构成投资建议，不执行自动交易。</span>
        <span className="mx-2">/</span>
        <Link className="font-semibold text-primary-700" href="/api-docs">
          公开 API
        </Link>
        <span className="mx-2">/</span>
        <Link className="font-semibold text-primary-700" href="/lifecycle/replay">
          生命周期回放
        </Link>
      </footer>
    </div>
  );
}
