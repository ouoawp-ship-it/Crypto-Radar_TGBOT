import { Header } from "./Header";

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-dvh bg-surface-canvas">
      <a className="sr-only z-50 rounded-md bg-surface-panel px-3 py-2 text-sm font-semibold text-text-primary focus:not-sr-only focus:fixed focus:left-3 focus:top-3" href="#main-content">跳到主要内容</a>
      <Header />
      <div className="mx-auto w-full max-w-[1280px] px-3 pb-28 pt-4 sm:px-5 md:pb-10 lg:px-6">
        <main className="min-w-0" id="main-content" tabIndex={-1}>{children}</main>
      </div>
      <footer className="hidden border-t border-border-subtle bg-surface-low px-4 py-4 text-center text-[11px] text-text-muted md:block">
        <span>Paoxx 仅整理市场事实与风险信号，不构成投资建议，不执行自动交易。</span>
      </footer>
    </div>
  );
}
