import type { Metadata } from "next";
import type { ReactNode } from "react";
import { GeistSans } from "geist/font/sans";
import Script from "next/script";
import "@/styles/globals.css";
import { AppShell } from "@/components/AppShell";
import { FrontendTelemetry } from "@/components/FrontendTelemetry";

export const metadata: Metadata = {
  title: "Paoxx 信号雷达",
  description: "极简加密市场信号与雷达运行看板。",
  other: {
    "paoxx-frontend": "nextjs-dashboard"
  }
};

const themeInitializer = `try{var saved=localStorage.getItem("paoxx.theme.v1");var theme=saved==="dark"||saved==="light"?saved:(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.dataset.theme=theme;document.documentElement.style.colorScheme=theme}catch(e){}`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN" className={GeistSans.variable} suppressHydrationWarning>
      <body data-paoxx-frontend="nextjs-dashboard">
        <Script id="paoxx-theme" strategy="beforeInteractive">{themeInitializer}</Script>
        <FrontendTelemetry />
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
