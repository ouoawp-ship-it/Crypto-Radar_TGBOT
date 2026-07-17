import Link from "next/link";
import { PageTitle } from "@/components/PageTitle";

const principles = [
  ["证据优先", "只读取 Paoxx 已验证的结构化市场事实，不复制第三方选币结论。"],
  ["规则先行", "方向、风险和数据质量先由确定性规则约束，再进入表达层。"],
  ["结论可追溯", "未来每条智选结果都必须能回到信号、时间、来源与反向证据。"],
  ["默认不下单", "产品只辅助观察与验证，不生成自动交易指令，也不读取交易私钥。"],
];

const reservedModules = [
  { code: "01", title: "市场结构摘要", text: "压缩市场广度、主流币状态、资金偏向和异常密度。" },
  { code: "02", title: "候选信号排序", text: "基于 Paoxx 自有规则与样本质量对信号分层，不复刻外部模型。" },
  { code: "03", title: "证据与反证", text: "并列显示支持条件、冲突条件、数据缺口与有效期。" },
];

export default function AgentsPage() {
  return (
    <div className="space-y-4" data-testid="paoxx-ai-reserved">
      <PageTitle
        title="泡泡智选"
        subtitle="该入口只预留给 Paoxx 自有版本；当前不提供第三方 AI 智选、荐币或自动交易功能。"
        tags={["PAOXX NATIVE", "功能预留", "不构成投资建议"]}
      />

      <section className="cockpit-panel overflow-hidden">
        <div className="grid lg:grid-cols-[1.2fr_0.8fr]">
          <div className="border-b border-border-subtle p-5 sm:p-7 lg:border-b-0 lg:border-r">
            <div className="flex items-center gap-2 text-[10px] font-bold tracking-[0.14em] text-primary-500">
              <span className="h-1.5 w-1.5 rounded-full bg-primary-500" /> IN DEVELOPMENT
            </div>
            <h2 className="mt-4 max-w-xl text-2xl font-bold tracking-[-0.03em] text-text-primary sm:text-3xl">先把证据做对，再让模型开口。</h2>
            <p className="mt-3 max-w-2xl text-sm leading-6 text-text-secondary">
              泡泡智选将建立在现有雷达、资金、信息和单币证据之上。当前页面不请求 AI 决策接口，也不会显示伪造候选；上线前会单独公布数据口径、门禁和验收结果。
            </p>
            <div className="mt-6 flex flex-wrap gap-2">
              <Link className="btn" href="/radar">先看实时雷达</Link>
              <Link className="btn-secondary" href="/info">查看信息证据</Link>
            </div>
          </div>

          <div className="bg-surface-low p-5 sm:p-7">
            <div className="text-[10px] font-bold tracking-[0.12em] text-text-muted">RELEASE GATE</div>
            <dl className="mt-4 divide-y divide-border-subtle">
              {[
                ["数据覆盖", "待验证"],
                ["回测样本", "待验证"],
                ["风险门禁", "设计中"],
                ["公开版本", "未开放"],
              ].map(([label, value]) => (
                <div className="flex items-center justify-between gap-4 py-3" key={label}>
                  <dt className="text-xs text-text-muted">{label}</dt>
                  <dd className="font-mono text-xs font-semibold text-warn">{value}</dd>
                </div>
              ))}
            </dl>
          </div>
        </div>
      </section>

      <section className="grid gap-3 lg:grid-cols-3">
        {reservedModules.map((module) => (
          <article className="cockpit-panel p-4" key={module.code}>
            <div className="font-mono text-[10px] font-semibold text-primary-500">{module.code}</div>
            <h2 className="mt-3 text-sm font-semibold text-text-primary">{module.title}</h2>
            <p className="mt-2 text-xs leading-5 text-text-secondary">{module.text}</p>
          </article>
        ))}
      </section>

      <section className="cockpit-panel p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-sm font-semibold text-text-primary">自有版本原则</h2>
          <span className="rounded-sm border border-border-subtle bg-surface-low px-2 py-1 font-mono text-[9px] text-text-muted">SPEC RESERVED</span>
        </div>
        <div className="grid gap-px overflow-hidden rounded-md border border-border-subtle bg-border-subtle sm:grid-cols-2">
          {principles.map(([title, text]) => (
            <div className="bg-surface-low p-4" key={title}>
              <h3 className="text-xs font-semibold text-text-primary">{title}</h3>
              <p className="mt-1.5 text-[11px] leading-5 text-text-secondary">{text}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
