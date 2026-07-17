export default function Loading() {
  return (
    <div aria-busy="true" aria-live="polite" className="space-y-5" role="status">
      <span className="sr-only">正在加载页面数据</span>
      <div className="space-y-3 py-2" aria-hidden="true">
        <div className="h-7 w-36 animate-pulse rounded-md bg-surface-container" />
        <div className="h-4 max-w-xl animate-pulse rounded bg-surface-container-low" />
        <div className="flex gap-2">
          <div className="h-7 w-20 animate-pulse rounded-full bg-surface-container-low" />
          <div className="h-7 w-24 animate-pulse rounded-full bg-surface-container-low" />
        </div>
      </div>
      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-hidden="true">
        {Array.from({ length: 4 }).map((_, index) => (
          <div className="panel h-28 animate-pulse bg-surface-container-low" key={index} />
        ))}
      </section>
      <section className="panel space-y-4 p-4 sm:p-5" aria-hidden="true">
        <div className="flex items-center justify-between gap-4">
          <div className="h-5 w-28 animate-pulse rounded bg-surface-container" />
          <div className="h-10 w-24 animate-pulse rounded-lg bg-surface-container" />
        </div>
        <div className="grid gap-3 xl:grid-cols-2">
          <div className="h-40 animate-pulse rounded-lg bg-surface-container-low" />
          <div className="h-40 animate-pulse rounded-lg bg-surface-container-low" />
        </div>
      </section>
    </div>
  );
}
