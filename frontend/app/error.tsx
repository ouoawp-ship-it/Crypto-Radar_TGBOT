"use client";

import { useEffect } from "react";
import { reportPublicTelemetry } from "@/lib/api";

export default function AppError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => reportPublicTelemetry("frontend_render_error"), []);
  return (
    <section className="panel border-risk/25 bg-risk/5 p-6">
      <h1 className="text-lg font-semibold text-red-700">页面暂时无法显示</h1>
      <p className="mt-2 text-sm leading-6 text-red-700/80">错误已按匿名计数记录，不会上传输入内容或账户信息。可以重新尝试，或返回信号雷达。</p>
      <div className="mt-4 flex gap-2"><button className="btn" onClick={reset}>重新尝试</button><a className="btn-secondary" href="/radar">返回信号雷达</a></div>
    </section>
  );
}
