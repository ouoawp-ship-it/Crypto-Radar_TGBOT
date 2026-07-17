import Link from "next/link";
import { PageTitle } from "./PageTitle";

export function FeatureUnavailable({ title }: { title: string }) {
  return (
    <div className="space-y-3">
      <PageTitle title={title} subtitle="V2 驾驶舱当前已通过回滚开关停用，Telegram Bot 与兼容信号接口仍正常运行。" tags={["ROLLBACK MODE", "兼容 API 可用"]} />
      <section className="cockpit-panel px-5 py-20 text-center">
        <h2 className="text-base font-semibold text-text-primary">当前处于安全回滚模式</h2>
        <p className="mx-auto mt-2 max-w-xl text-sm leading-6 text-text-muted">管理员可以在完成数据和服务复核后重新启用 V2；关闭期间不会删除任何历史数据。</p>
        <div className="mt-6 flex flex-wrap justify-center gap-2"><Link className="btn" href="/radar">打开兼容信号雷达</Link><Link className="btn-secondary" href="/">返回总览</Link></div>
      </section>
    </div>
  );
}
