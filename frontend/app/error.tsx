"use client";

export default function ErrorPage({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <section className="panel border-risk/30 bg-risk/10 p-8 text-center">
      <h1 className="text-2xl font-black text-red-100">页面加载失败</h1>
      <p className="mt-3 text-sm text-red-100/80">数据暂时不可用，请稍后重试。</p>
      <button className="btn mt-5" onClick={() => reset()}>
        重试
      </button>
    </section>
  );
}
