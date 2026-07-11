import { PageTitle } from "@/components/PageTitle";

const groups = [
  {
    title: "信号接口",
    items: ["/public-api/signals", "/public-api/signals/detail", "/public-api/signals/stats", "/public-api/signal-timeline"]
  },
  {
    title: "决策接口",
    items: ["/public-api/decision", "/public-api/decisions", "/public-api/decisions/stats"]
  },
  {
    title: "结果追踪接口",
    items: ["/public-api/outcomes", "/public-api/outcomes/stats", "/public-api/symbol-outcomes"]
  },
  {
    title: "决策回测接口",
    items: ["/public-api/backtest/decision", "/public-api/backtest/decision/matrix", "/public-api/backtest/decision/detail"]
  },
  {
    title: "生命周期接口",
    items: [
      "/public-api/lifecycle/summary",
      "/public-api/lifecycle/list",
      "/public-api/lifecycle/detail?symbol=BTCUSDT",
      "/public-api/lifecycle/events?symbol=BTCUSDT",
      "/public-api/lifecycle/metrics?symbol=BTCUSDT"
    ]
  },
  {
    title: "生命周期智能与回放",
    items: [
      "/public-api/lifecycle/intelligence/summary",
      "/public-api/lifecycle/intelligence/list?limit=10",
      "/public-api/lifecycle/intelligence/detail?symbol=BTCUSDT",
      "/public-api/lifecycle/replay?symbol=BTCUSDT",
      "/public-api/lifecycle/replay/frames?symbol=BTCUSDT&limit=20",
      "/public-api/lifecycle/analytics/first-level",
      "/public-api/lifecycle/analytics/upgrade-path",
      "/public-api/lifecycle/analytics/module",
      "/public-api/lifecycle/analytics/capital-confirmation",
      "/public-api/lifecycle/similar?symbol=BTCUSDT&limit=5"
    ]
  },
  {
    title: "生命周期 Outcome 数据质量",
    items: [
      "/public-api/lifecycle/outcomes/summary",
      "/public-api/lifecycle/outcomes/coverage?limit=10",
      "/public-api/lifecycle/outcomes/list?limit=10",
      "/public-api/lifecycle/outcomes/detail?symbol=BTCUSDT",
      "/public-api/lifecycle/outcomes/reasons",
      "/public-api/lifecycle/outcomes/maturity"
    ]
  }
];

export default function ApiDocsPage() {
  return (
    <div className="space-y-5">
      <PageTitle title="公开 API" subtitle="公开前台只读取脱敏后的只读接口，不需要后台登录。" tags={["只读", "脱敏", "同域访问"]} />
      <section className="grid gap-4 md:grid-cols-2">
        {groups.map((group) => (
          <div className="panel p-5" key={group.title}>
            <h2 className="text-lg font-black text-white">{group.title}</h2>
            <div className="mt-4 space-y-2">
              {group.items.map((item) => (
                <code className="block rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-cyan-100" key={item}>
                  {item}
                </code>
              ))}
            </div>
          </div>
        ))}
      </section>
      <section className="panel p-5 text-sm leading-7 text-slate-300">
        <h2 className="mb-3 text-lg font-black text-white">脱敏说明</h2>
        <p>
          公开 API 不返回后台配置、Telegram 私有字段、密钥、审计、日志、Cookie、Authorization、chat_id、api_key、payload_json、text_html、dedup_key、message_ids、topic_id 或 reply_to_message_id。
        </p>
        <p className="mt-3">公开数据仅用于信号展示、风险提示和复盘统计；不构成投资建议，不执行自动交易。</p>
        <p className="mt-3">Lifecycle Outcome 的关联覆盖率与数据成熟度是两个不同指标：尚未到期不是失败，pending 不是失败，unavailable 不等于亏损；只有 success Outcome 参与成熟收益统计。</p>
      </section>
    </div>
  );
}
