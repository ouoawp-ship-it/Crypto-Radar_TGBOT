# OAR 生产级持续异常发现升级路线图

## 工程目标

将链上活动雷达升级为可持续运行、可解释、可灰度和可回滚的异常发现系统。升级顺序固定为：

1. 证明市场雷达与投递链路的实际状态；
2. 提升已审核地址标签覆盖，并保留未知流向；
3. 以真实已发送市场信号构建动态 Watchlist；
4. 用完整扫描建立多窗口历史基线；
5. 只读计算链上与市场共振；
6. 通过显式开关启用受控自动预警门禁；
7. 在逐链验收后扩展 EVM 链；
8. 只在证据完整时提供谨慎 AI 解释。

任何阶段都不得以 AI、行为相似或 Symbol 猜测替代链上事实、合约审核或地址身份审核。

## 永久安全约束

- 主 BOT 默认 `dry_run`，OAR Watch 默认 `observe`。
- 真实 Telegram 仍必须经过现有 `--send --confirm-real-send` 双门禁及配置确认。
- Watch 热路径不调用 Dune、Arkham 或其他外部标签 Provider。
- 不完整查询不进入历史基线，不形成高确定性判断。
- 标签覆盖不足时保留 Transfer 事实，方向为 `unclassified`。
- AI 不改变确定性规则摘要，不生成地址身份，不替代缺失证据。
- 所有新增网络路径必须有超时、请求预算和固定脱敏错误码。
- 每次部署前创建恢复点；每次灰度结束恢复安全模式。

## 阶段 0：四个市场雷达与投递诊断

范围包括启动预警、资金摘要、资金费率警报和五因子资金流雷达。

验收：

- `paopao radar-status` 只读取本地状态，不访问网络；
- 四个雷达分别显示最近运行、下次调度、候选数和最近投递状态；
- `delivery_block_reason=main_bot_dry_run` 表示仍在计算，但真实 Telegram 被安全模式阻止；
- 运行状态过期时不得显示为 `running`；
- 异常只输出固定类型或错误码，不输出 Provider 正文、URL 或凭据。

灰度：先部署诊断命令，不改变主 BOT 模式；观察至少一个完整调度周期。

回滚：回退应用提交即可；不回滚或删除运行历史和业务数据库。

## 阶段 1：标签覆盖与动态 Watchlist

生产分类仅使用已人工批准且未冲突、未过期的标签。标签可来自人工、Dune、OLI、BaseScan、官方来源或可选 Arkham，但必须保留 `source`、置信度、有效期和证据哈希。

动态 Watchlist 只接受主 BOT 中 `status=sent`、`sent=1` 且满足结构和质量门禁的真实市场信号。Dry-run、skipped、blocked 和失败记录不得创建 Watch 来源。

灰度：先只观察未知地址队列与 Signal Bridge 审计，不自动批准标签，不扩大 Active Watch。

回滚：禁用新增来源或撤销单条审核标签；不得删除 Registry、Watch 或 Scan Audit 历史。

## 阶段 2：多窗口历史异常基线

只使用同一 Token、同一窗口且 Token Activity、Behavior Analysis 和 Scan Audit 均完整成功的扫描。基线指标包括 Transfer、Token 总量、独立收发钱包和确定性行为分。

验收：

- 冷启动样本不足时为 `cold_start`，不得产生异常结论；
- 15m、1h、4h、24h 分开统计；
- Partial、failed、stale 不污染样本；
- 本地计算失败不影响扫描和 Lease 释放；
- `watch-baseline` 为零网络、零 AI、零 Telegram 的只读命令。

灰度：先积累至少配置要求的完整样本，仅观察 `anomaly` 与 `multi_window_anomaly`。

回滚：关闭后续受控预警门禁；保留历史审计，不手工编辑 SQLite。

## 阶段 3：链上与市场共振

共振只使用 Signal Bridge 已接受的真实市场信号，与本轮链上规则和历史异常进行确定性共现计算。第一版不从中文摘要猜测多空方向，`direction_alignment=not_evaluated`。

验收：共振分明确标注为规则分而非概率，并保持 `notification_gate_changed=false`。

灰度：只记录诊断，不改变通知行为。

回滚：停止消费共振字段；不删除原始市场信号或链上 Scan Audit。

## 阶段 4：受控自动预警

默认 `OAR_WATCH_CONTROLLED_ALERT_ENABLE=false`。显式启用后，完整扫描、原有链上规则、成熟历史基线、本轮异常和受支持市场来源必须同时满足，才允许进入现有报告与通知流程。

该开关不会自动打开 AI、Dry-run、Real 或 Telegram；真实发送仍由 Delivery Mode 和双门禁控制。

灰度顺序：Observe → Telegram Dry-run → 单次人工真实发送。长期 Real 必须另行授权。

回滚：将开关恢复为 `false` 并显式重启 OAR Watch；确认 Worker=1、Lease 释放、Telegram HTTP=0。

## 阶段 5：多链 EVM

新增链必须在版本化链注册表中配置唯一 Chain ID、slug、确认深度、回看范围、专用 RPC 环境变量和 Explorer 模板。只有逐链 RPC、Registry、预算、重组、灰度和恢复点全部通过后才能启用 Watch。

Base 地址情报命名空间不得接收其他链地址；未支持的地址情报队列应安全跳过，外部标签 Provider 调用保持 0。

回滚：禁用单链注册项并停止该链 Watch 来源，不影响 Base 与其他已验收链。

## 阶段 6：谨慎 AI 解释

AI 仅在显式请求、总开关开启、配置完整且输入证据满足门禁时调用。查询不完整、行为证据不足、CEX 覆盖不足或市场方向未结构化时必须使用受限输入，并只接受 `neutral/uncertain + low`。

灰度：先合成 Smoke，再单次真实上下文 Dry-run；不得直接进入长期自动 AI。

回滚：恢复 `OAR_AI_ENABLE=false` 和 `OAR_WATCH_WITH_AI=false`；规则报告继续可用。

## 每阶段发布门禁

每个阶段必须依次完成：

1. 独立、可审阅的代码与测试；
2. Draft PR 和 CI；
3. 无未解决 Review Thread；
4. Squash Merge，不使用管理员绕过；
5. 服务器一致性恢复点；
6. fast-forward 部署；
7. Observe 或 Dry-run 灰度；
8. 数据库、Worker、Lease、日志和凭据检查；
9. 明确的回滚演练；
10. 最终安全配置复核。

本地提交、PR 合并、服务器部署和生产验收是四种不同状态。报告必须分别陈述，不得用测试通过代替生产运行证据。
