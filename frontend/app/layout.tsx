import type { Metadata } from "next";
import type { ReactNode } from "react";
import "@/styles/globals.css";
import { AppShell } from "@/components/AppShell";

export const metadata: Metadata = {
  title: "Paoxx 信号雷达",
  description: "加密市场信号、决策、结果追踪与回测仪表盘"
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
