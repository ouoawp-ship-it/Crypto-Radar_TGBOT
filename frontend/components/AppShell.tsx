import { Header } from "./Header";

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-dvh bg-surface-canvas">
      <a className="sr-only z-50 rounded-md bg-surface-panel px-3 py-2 text-sm font-semibold text-text-primary focus:not-sr-only focus:fixed focus:left-3 focus:top-3" href="#main-content">跳到主要内容</a>
      <Header />
      <div className="mx-auto w-full max-w-[1920px] px-3 pb-8 pt-3 sm:px-4 lg:px-5">
        <main className="min-w-0" id="main-content" tabIndex={-1}>{children}</main>
      </div>
      <footer className="border-t border-border-subtle bg-surface-panel px-4 py-5 text-center text-xs text-text-muted">
        <span>仅用于市场信号整理与风险提示，不构成投资建议，不执行自动交易。</span>
      </footer>
    </div>
  );
}
