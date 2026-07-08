"use client";

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="panel border-risk/30 bg-risk/10 p-6">
      <div className="font-black text-red-200">加载失败</div>
      <p className="mt-2 text-sm text-red-100/80">{message || "数据暂时不可用，请稍后重试。"}</p>
      {onRetry ? (
        <button className="btn mt-4" onClick={onRetry}>
          重试
        </button>
      ) : null}
    </div>
  );
}
