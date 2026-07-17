"use client";

export function ErrorState({
  message,
  onRetry,
  retainedData = false
}: {
  message: string;
  onRetry?: () => void;
  retainedData?: boolean;
}) {
  return (
    <div aria-live="assertive" className="panel border-risk/25 bg-risk/5 p-6" role="alert">
      <div className="font-semibold text-red-700">加载失败</div>
      <p className="mt-2 text-sm text-red-700/80">{message || "数据暂时不可用，请稍后重试。"}</p>
      {retainedData ? <p className="mt-2 text-sm font-medium text-red-700">当前仍显示上次成功数据，内容可能已过期。</p> : null}
      {onRetry ? (
        <button className="btn mt-4" onClick={onRetry} type="button">
          重试
        </button>
      ) : null}
    </div>
  );
}
