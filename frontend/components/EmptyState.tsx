export function EmptyState({
  title = "暂无数据",
  text = "可以稍后刷新，或调整筛选条件。"
}: {
  title?: string;
  text?: string;
}) {
  return (
    <div className="panel p-6 text-center">
      <div className="text-sm font-black text-slate-200">{title}</div>
      <p className="mt-2 text-sm text-slate-500">{text}</p>
    </div>
  );
}
