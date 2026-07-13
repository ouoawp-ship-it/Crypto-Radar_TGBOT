import type { Metadata } from "next";
import type { ReactNode } from "react";
import { GeistSans } from "geist/font/sans";
import "@/styles/globals.css";
import { AppShell } from "@/components/AppShell";

export const metadata: Metadata = {
  title: "Paoxx 信号雷达",
  description: "极简加密市场信号与雷达运行看板。",
  other: {
    "paoxx-frontend": "nextjs-dashboard"
  }
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN" className={GeistSans.variable}>
      <body data-paoxx-frontend="nextjs-dashboard">
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
