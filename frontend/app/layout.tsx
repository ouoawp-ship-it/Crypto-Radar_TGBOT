import type { Metadata } from "next";
import type { ReactNode } from "react";
import { GeistSans } from "geist/font/sans";
import "@fontsource-variable/dm-sans";
import "@fontsource-variable/jetbrains-mono";
import "@/styles/globals.css";
import { AppShell } from "@/components/AppShell";
import { FrontendTelemetry } from "@/components/FrontendTelemetry";

export const metadata: Metadata = {
  title: "Paoxx 市场雷达",
  description: "面向交易员的市场异常、资金、信息与信号证据驾驶舱。",
  other: {
    "paoxx-frontend": "nextjs-dashboard"
  }
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html data-theme="dark" lang="zh-CN" className={GeistSans.variable} style={{ colorScheme: "dark" }}>
      <body data-paoxx-frontend="nextjs-dashboard">
        <FrontendTelemetry />
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
