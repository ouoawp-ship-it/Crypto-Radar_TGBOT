import { Header } from "./Header";

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-surface-canvas">
      <Header />
      <div className="mx-auto w-full max-w-[1920px] px-3 pb-8 pt-3 sm:px-4 lg:px-5">
        <main className="min-w-0">{children}</main>
      </div>
      <footer className="border-t border-border-subtle bg-surface-panel px-4 py-5 text-center text-xs text-text-muted">
        <span>仅用于市场信号整理与风险提示，不构成投资建议，不执行自动交易。</span>
      </footer>
    </div>
  );
}
