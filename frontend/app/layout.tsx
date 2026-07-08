import type { Metadata } from "next";
import type { ReactNode } from "react";
import "@/styles/globals.css";
import { AppShell } from "@/components/AppShell";

export const metadata: Metadata = {
  title: "Paoxx 信号雷达",
  description: "专业加密数据仪表盘，聚合信号、决策、结果追踪与回测统计。",
  other: {
    "paoxx-frontend": "nextjs-dashboard"
  }
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body data-paoxx-frontend="nextjs-dashboard">
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
