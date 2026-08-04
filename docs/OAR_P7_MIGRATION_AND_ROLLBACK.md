# OAR P7A 迁移与回滚

## 数据变化

P7A **没有 SQLite Schema 变化、没有历史数据搬迁、没有双写、没有新生产数据库**。
`onchain_flow.db` migrations 仍为 1–3，`oar_automation.db` 仍为 schema 6，独立
`onchain_signals.db` 仍使用 SignalEventStore schema 6。新增信号和快照在查询结果内产生；
功能默认关闭时没有额外 RPC 和持久化。

## 配置变化

新增 Single Transfer Risk，以及 BSC、Ethereum、Arbitrum、Optimism、Polygon、Avalanche
C-Chain 的 allowlisted keys。旧 `.env.onchain` 缺少这些键时，加载值安全默认为 disabled。
任一新增链 enable=true 但 RPC 缺失、URL 不安全或阈值组合不合法时，
ConfigManager 拒绝保存并恢复旧文件。

## 推荐上线顺序（后续阶段，不在本轮执行）

1. 备份 `.env.onchain`、chain registry 和所有链上 SQLite/JSON/私有 CSV；逐库执行
   `PRAGMA quick_check`。
2. fast-forward 更新代码，但保持 `OAR_SINGLE_TRANSFER_RISK_ENABLE=false`、
   所有新增链 enable=false、OAR observe、AI/Real 关闭。
3. 运行离线配置/chain readiness/formatter/fixture 测试。
4. Base 只读 request diagnostics，确认新增 RPC phase 在禁用时为 0。
5. P7B 每次只为一条链配置审核 RPC，并从 readiness、一次查询、前台 Observe 逐级灰度；
   不并行启用多条未灰度链。
6. Single Transfer Risk 先 Observe 记录证据，不直接开启真实投递。

## 回滚

- 代码：回退到 P7A 前 release；旧库 Schema 未变化，可直接读取。
- 配置：使用 ConfigManager 备份恢复，或将两个 enable 设为 false；不删除数据库。
- chain registry：恢复备份的 v1 registry；Base slug/chain ID/旧字段仍兼容。
- 数据：P7A 没有历史迁移，无反向 migration；独立历史、Outbox、Registry、Watch、Baseline
  和候选审核全部保留。

## 后续 facts migration 门禁

创建 `onchain_facts.db` 前必须单独设计：source/target quick_check、event identity 去重、逐表
计数和 hash 抽样、原子切换、旧库只读期、双读对比、增长上限和 rollback reader。未经这些
门禁，不得把现有 `onchain_flow.db` 标为可删除。
