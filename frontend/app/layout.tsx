import type { Metadata } from "next";
import type { ReactNode } from "react";
import { DM_Sans, JetBrains_Mono } from "next/font/google";
import "@/styles/globals.css";
import { AppShell } from "@/components/AppShell";
import { FrontendTelemetry } from "@/components/FrontendTelemetry";

const dmSans = DM_Sans({
  display: "swap",
  subsets: ["latin"],
  variable: "--font-dm-sans",
  weight: ["400", "500", "600", "700"]
});

const jetbrainsMono = JetBrains_Mono({
  display: "swap",
  subsets: ["latin"],
  variable: "--font-jetbrains-mono",
  weight: ["400", "500", "600", "700"]
});

export const metadata: Metadata = {
  title: "Paoxx 市场雷达",
  description: "面向交易员的市场异动、资金、信息与信号证据工作站。",
  other: { "paoxx-frontend": "nextjs-dashboard" }
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html data-theme="light" lang="zh-CN" className={`${dmSans.variable} ${jetbrainsMono.variable}`} style={{ colorScheme: "light" }}>
      <body data-paoxx-frontend="nextjs-dashboard">
        <FrontendTelemetry />
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
