import Link from "next/link";

export default function NotFound() {
  return (
    <section className="panel p-8 text-center">
      <h1 className="text-2xl font-black text-white">页面不存在</h1>
      <p className="mt-3 text-sm text-slate-400">当前公开前台没有这个页面，可以返回总览或查看信号雷达。</p>
      <div className="mt-5 flex justify-center gap-3">
        <Link className="btn" href="/">
          返回总览
        </Link>
        <Link className="btn" href="/radar">
          查看信号雷达
        </Link>
      </div>
    </section>
  );
}
