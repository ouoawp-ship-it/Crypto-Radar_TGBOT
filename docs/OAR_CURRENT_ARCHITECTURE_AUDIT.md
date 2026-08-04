# OAR 当前真实架构审计

审计基线：`f9edeb9b2427bfabe3ee617644d695cffabc112e`。本报告以该提交及
P7 本地收敛差异的实际源码、CLI 注册、systemd Unit 和测试为依据；历史阶段文档只用于
交叉核对，不作为事实来源。

## 1. 入口

生产链上入口是 `onchain_main.py`，它只委托给
`paopao_radar.onchain_flow.cli.main()`，没有接入主 BOT 的 `main.py live/loop`。

当前 CLI 按职责分为：

- 状态和安全检查：`status`、`doctor`、`db-check`、`labels-check`、
  `chain-readiness`、`provider-check`、`cursor-status`；
- 事实和分析：`token-activity`、`token-analysis`、`token-report`、
  `token-notify`、`ai-request-check`；
- Token Registry：`registry-add`、`registry-verify`、`registry-list`、
  `registry-disable`；
- Watch/Bridge：`watch-add`、`watch-list`、`watch-remove`、`watch-once`、
  `watch-live`、`watch-baseline`、`bridge-once`、`unresolved-summary`；
- 地址情报：`address-intelligence providers|queue|discover|candidates|status|approved|
  approve|reject|defer|import-dune|import-oli|import-basescan`，以及仍兼容旧 Arkham
  流程的 `label-candidates`；
- AI：`ai-prompt`、`ai-prompt-check`、`ai-cache`、`ai-provider-check`、
  `ai-smoke`；
- Telegram：`telegram-topic-link`、`telegram-route-check`、`telegram-topic`、
  `telegram-query-live`；
- 旧实时收集器兼容入口：`once`、`daemon`、`live`、`replay`。

Token Activity 由 `TokenActivityQueryService.query()` 完成；Token Analysis 由
`TokenAnalysisService.analyze()` 组合 P2 行为规则和钱包群组；报告与通知分别由
`TokenReportService` 和 `ReportNotifier` 完成。Registry、Watch、Bridge、群内查询、
地址情报、历史基线和路由检查均是独立入口，不共享主 BOT 写路径。

## 2. 核心模块

下表中的“生产”表示存在当前 CLI/systemd 入口，不表示所有功能都已启用。

| 文件 | 主要类/函数 | 输入 → 输出 | 网络/写入 | 生产、重复与测试 |
|---|---|---|---|---|
| `models.py` | `NormalizedTransfer`、`AddressLabel`、`ClassifiedFlow`、窗口/告警模型 | RPC/fixture 字段 → 规范事实和派生对象 | 无网络；无写入 | 生产；Canonical Event Identity 为 `chain_id:tx_hash:log_index`；广泛测试 |
| `collectors/evm_http.py` | `JsonRpcClient`、`BaseHttpCollector`、Adaptive Range | EVM JSON-RPC → 校验后的日志、区块和调用结果 | 有界 RPC；不直接写库 | 生产；通用 EVM 实现，名称仍含 Base；RPC/重组/预算测试完整 |
| `collectors/evm_ws.py` | `WssHeadTrigger` | WSS head → 触发器事件 | WSS；无写入 | 旧实时收集器生产兼容；有 runtime 测试 |
| `collectors/replay.py` | `ReplayCollector` | fixture → 事实流 | 零网络；无生产写入 | 测试/离线回放；保留 |
| `token_metadata.py` | `TokenMetadataResolver` | 合约 + block tag → symbol/name/decimals/supply | 有界 `eth_call`；无写入 | Token Activity 生产；metadata 测试 |
| `token_activity.py` | `TokenActivityQueryService`、`BlockHeaderCache` | chain/contract/window/budget → Transfer 事实、分类、完整性、RPC 分阶段统计 | 显式 `--allow-network` RPC；查询模式不写业务库 | 生产；P1 核心；大量预算/partial/跨链测试 |
| `classifier.py` | `classify_transfer`、`ReviewedLabelTransferClassifier` | Transfer + 链级标签 → inflow/outflow/internal/cross-CEX/unclassified | 无网络/写入 | 生产；只有 reviewed/eligible 标签参与；分类测试 |
| `labels.py` | `normalize_evm_address`、`LabelRegistry`、`ApprovedAddressLabelRepository` | CSV → 链隔离标签 | 只读 CSV；无网络 | 生产；地址标准化权威入口；标签安全测试 |
| `behavior.py` | `BehaviorAnalyzer` | 完整窗口事实 → 持续归集/派发候选和规则证据 | 无网络/写入 | 生产 P2；不证明现实身份；行为边界测试 |
| `wallet_groups.py` | `WalletGroupAnalyzer` | Transfer 图 → 确定性群组候选 | 无网络/写入 | 生产 P2；评分不是概率；群组测试 |
| `token_analysis.py` | `TokenAnalysisService` | Token Activity → 行为/钱包分析 | 通过注入的事实 Provider；无直接写入 | 生产；已改为领域接口依赖；分析测试 |
| `token_snapshots.py` | `EvmTokenSnapshotProvider` | Transfer + block → before/after balance、totalSupply、可选 circulating supply | 有界 `eth_call`；TTL 内存缓存；无写入 | P7 implemented_not_deployed；全部 EVM 链共用；定向测试 |
| `single_transfer_risk.py` | `SingleTransferRiskEngine` | 标准 Transfer + 快照/身份/历史/反证 → 分离的确定性信号 | 纯函数式，无网络/写入 | P7 implemented_not_deployed；默认关闭；边界测试 |
| `single_transfer_service.py` | `SingleTransferRiskService` | Token Activity Transfer 列表 + repositories → 单笔风险结果 | 只调用注入 Provider；无 Telegram/DB | P7 编排层；定向测试 |
| `domain.py` | 领域 dataclass 与最小 Protocol | 领域边界 | 无 IO | P7 收敛接口；不制造第二实现 |
| `signal_policy.py` | `DefaultSignalPolicy` | P2 分数或单笔风险 → actionable/gate reasons | 无 IO | Watch 生产策略；默认行为兼容；策略测试 |
| `scan_baseline.py` | `HistoricalScanBaseline`、MAD/嵌套窗口分析 | 完整 Scan Audit → median/MAD/连续覆盖 | 通过 AutomationStore 写基线 | 生产 P6；partial 不进入；基线测试 |
| `automation_store.py` | `AutomationStore` | Registry/Watch/Lease/Audit/Baseline/Rolling | 独立 SQLite 写入 | 生产，schema 6；迁移、lease、watch 测试 |
| `watch_scanner.py` | `WatchScanner` | Active Watch → query/analyze/baseline/policy/audit | 可调用链 RPC；地址 Provider/AI/Telegram 按门禁为 0 | 生产 systemd；单 Worker/Lease/降级测试 |
| `signal_bridge.py` | `MainSignalReader`、`SignalBridge` | 主 `signals.db` 只读事件 → Watch source/unresolved audit | SQLite `mode=ro` + `query_only`；只写 Automation DB | 生产；主库隔离测试 |
| `address_intelligence.py` | Provider 接口、Store、Service、Dune/OLI/Arkham/manual/behavior | 本地未知队列或显式导入 → pending/reviewed 候选 | 仅显式命令可联网；JSON/私有 CSV 原子写 | 生产运维；Watch 热路径零 Provider；大量 P5E 测试 |
| `label_candidates.py` | 旧 Arkham 候选 Store/Discovery | 地址 → Arkham exact evidence candidate | 显式网络；独立 JSON/CSV 写 | 兼容入口；与通用地址情报部分重合，待迁移；仍有 CLI/测试 |
| `ai_context.py`、`ai_client.py` | context builder、`OpenAiCompatibleOarClient`、cache | 受限分析 → 九字段 JSON | 显式/门禁后 HTTP；独立 cache JSON | 生产可选，默认关闭；错误脱敏/超时/Schema 测试 |
| `report.py`、`report_formatter.py` | 规则摘要、`TokenReportService`、中文报告 | Analysis + 可选 AI → report dict/text | AI 由注入 client；formatter 无 IO | 生产；已注册 EVM explorer 支持；报告测试 |
| `signal_formatter.py` | `OarSignalCardFormatter` | 确定性信号 fixture → 8 类中文卡片 | formatter-only，零网络/写入 | P7 implemented_not_deployed；脱敏/措辞测试 |
| `report_notifier.py` | `ReportNotifier` | report → card lifecycle | 经 TelegramGateway；独立 history/outbox/signal DB | 生产；先持久化新卡再删旧卡；失败保旧测试 |
| `notifier.py`、`formatter.py` | 旧 collector alert formatter/notifier | `OnchainAlert` → Telegram alert | Telegram 受双门禁；链上独立文件写 | 旧实时入口兼容；与 Token Report 不是同一模型；有测试 |
| `telegram_query.py` | parser/client/state/service | 群内命令 → Registry 查询回复 | 独立 Worker；显式真实发送门禁；状态 JSON | 生产；轮询、冷却、解析测试 |
| `telegram_route_check.py`、`telegram_topic_link.py` | route checker/link parser | 已配置共享群 → 脱敏 readiness/topic | route check 只用非持久 Telegram API；JSON 可选写 | 生产运维；零持久消息测试 |
| `chain_capabilities.py` | `EvmChainSpec`、`ChainRef`、readiness/RPC resolver | versioned registry + settings → chain capability | readiness 默认零网络 | Base 生产；BSC、Ethereum、Arbitrum、Optimism、Polygon、Avalanche 为 P7 离线接入；跨链测试 |
| `config.py`、`scripts/paopao_config.py` | `OnchainSettings`、ConfigManager | `.env.onchain`/`.env.oi` → 已校验设置 | 无网络；锁/备份/原子替换 | 生产；敏感值只报 configured；配置回滚测试 |
| `db.py`、`migrations.py` | `OnchainStore`、迁移 1–3 | collector facts/windows/alerts → `onchain_flow.db` | SQLite 写 | 旧实时 collector 生产兼容；尚未迁到目标 facts repository |
| `aggregator.py`、`detector.py`、`scorer.py` | 滚动窗口/检测/评分 | collector facts → alert candidates | 无网络/直接写入 | 旧实时 pipeline 使用；不可误删；pipeline 测试 |
| `market_convergence.py` | `evaluate_market_convergence` | 只读市场事实 → 共振证据 | 无采集网络；不写主库 | 生产/P6；不重复市场采集；测试 |

## 3. 存储

| 存储 | 创建/读写方 | Schema/权威性 | 处置 |
|---|---|---|---|
| `data/onchain/onchain_flow.db` | `OnchainStore` + live/replay collector | migrations 1–3；旧 collector 的 Transfer、flow、rolling、alerts、cursor、价格缓存事实 | 仍有生产/回滚依赖，不删除；后续以 facts repository 迁移 |
| `data/onchain/oar_automation.db` | `AutomationStore` | schema 6；Registry、Watch source、Lease、Scan Audit、Baseline、Rolling coverage/events、未知地址队列 metadata 的权威库 | 保留 |
| `data/onchain/onchain_signals.db` | 通用 `SignalEventStore`，路径由 OAR 配置隔离 | schema 6；OAR SignalEvent/通知摘要权威库 | 保留；不得写主 `data/signals.db` |
| `data/signals.db` | 主 BOT | 主信号权威库 | OAR Bridge 只读；绝不迁入 OAR 写路径 |
| `config/onchain/cex_addresses.private.csv` | ConfigManager/人工审核 | reviewed 生产标签权威源；权限 600 | 保留；不得自动导入候选 |
| `data/onchain/address_intelligence.json` | `AddressIntelligenceStore` | schema 1；统一候选、冲突、审核状态和证据摘要 | 保留，权限 600；不存 Provider 原文 |
| `data/onchain/label_candidates.json` | 旧 `LabelCandidateStore` | schema 1；Arkham 兼容候选 | 保留兼容，后续单向迁入统一 Store 后弃用 |
| `data/onchain/tg_push_history.json` | TelegramGateway/ReportNotifier | 通知和卡片生命周期审计 | 权威投递历史；不可删除 |
| `data/onchain/tg_outbox.json` | TelegramGateway | delivery reservation/result | 权威 Outbox；不可删除 |
| `data/onchain/tg_topic_routes.json` | TelegramGateway | 话题路由和 intro 状态 | 生产路由状态；不可删除 |
| `data/onchain/telegram_query_state.json` | Telegram Query Worker | `oar-telegram-query-v1`；offset/lease/cooldown | Worker 权威状态；不可删除 |
| `data/onchain/telegram_query_history.json` / `telegram_query_outbox.json` | Query Worker gateway | 群内查询独立投递审计 | 与自动报告隔离；保留 |
| `data/onchain/oar_ai_cache.json` | `OarAiCache` | schema 1；只存结构化合规输出，不存 reasoning_content | 可按 TTL 清理，不是事实权威源 |
| `data/onchain/runtime_status.json` | `health.py`/live runtime | 运行状态快照，无业务事实权威性 | 可重建；保留运维用途 |
| `data/onchain/telegram_route_check.json` | route check | 脱敏 readiness 快照 | 可重建；权限 600 |
| `config/onchain/chains.json` 或 example fallback | 运维/源码 | versioned EVM chain registry | 配置权威；本轮 example 升至 v2 |
| `backups/...` | 运维脚本/ConfigManager | 部署/配置/SQLite 恢复点 | 不入 Git；按保留策略清理 |

本轮没有创建 `onchain_facts.db`，也没有搬迁历史：现存 `onchain_flow.db` 仍被 live
collector、replay、回滚文档和测试依赖。直接重命名或双写会扩大生产风险。P7 先以
`ChainFactProvider`/`TokenSnapshotProvider` 隔离新代码，再由后续迁移阶段建立目标库。

## 4. 服务与进程边界

- `paopao-oar-watch.service`：加载 `.env.onchain`，再可选加载 `.env.oi`；调用
  `scripts/run_oar_watch.sh`。observe 默认只运行 `watch-live --allow-network`；
  Dry-run/Real/AI 参数由包装器严格门禁。`KillSignal=SIGINT`、
  `SuccessExitStatus=130`、`Restart=on-failure`。
- `paopao-oar-query.service`：相同环境加载顺序；调用 `scripts/run_oar_query.sh`。
  只有 Query enable、固定 ACK 及 Telegram 配置完整时启动；独立 offset/state。
- `paopao-radar.service`：主 BOT，安全包装器在 dry-run 使用 `main.py loop`；其
  `signals.db` 对 OAR 只读。
- `paopao-market-stream.service`：市场快照采集；OAR convergence 只读其结果，不创建
  第二采集器。

Watch 的唯一写者由 systemd 单 Worker、Automation DB lease、owner token 和 lease
fencing 共同约束。查询命令不推进 live chain cursor；partial 结果不进入完整基线，也
不能覆盖已有完整卡片。

## 5. 当前能力矩阵

| 能力 | 状态 | 说明 |
|---|---|---|
| Base | production_ready | Registry、Token Activity/Analysis、Watch、报告已生产运行 |
| BSC | implemented_not_deployed | P7 共用 EVM readiness/registry/query/analysis/risk/explorer 骨架，默认关闭，未真实 RPC 灰度 |
| Ethereum | implemented_not_deployed | 共用 EVM Registry、Token Activity/Analysis、Watch、风险与 Explorer；默认关闭，未真实 RPC 灰度 |
| Arbitrum | implemented_not_deployed | 共用 EVM 适配器；默认关闭，未真实 RPC 灰度 |
| Optimism | implemented_not_deployed | 共用 EVM 适配器；默认关闭，未真实 RPC 灰度 |
| Polygon | implemented_not_deployed | 共用 EVM 适配器；默认关闭，未真实 RPC 灰度 |
| Avalanche C-Chain | implemented_not_deployed | 共用 EVM 适配器；默认关闭，未真实 RPC 灰度 |
| Solana | missing | 非 EVM 地址、事件定位与程序模型尚未实现，不复用 ERC-20 解析器 |
| Tron | missing | 非 EVM 地址编码和 Provider 尚未实现 |
| Bitcoin | missing | UTXO 事实模型、资产身份和事件定位尚未实现 |
| Sui | missing | Move object/event 模型和 Provider 尚未实现 |
| TON | missing | account/message/jetton 模型和 Provider 尚未实现 |
| Transfer | production_ready | 标准化、canonical identity、adaptive range、完整性和重组语义 |
| CEX Flow | production_ready | reviewed 标签下的 gross/net/internal/cross-CEX；覆盖不足时 unclassified |
| Single Transfer Risk | implemented_not_deployed | P7 引擎/快照/策略已实现，配置默认关闭 |
| Wallet Balance | implemented_not_deployed | 精确 block tag 的 before/after `balanceOf`，有界预算 |
| Supply Share | implemented_not_deployed | totalSupply share 可用；circulating share 仅在注入来源存在时计算 |
| Holder | missing | 无全量 holder indexer |
| LP | partial | 旧 collector 能存 pool/flow 相关事实，但无 P7 目标中的完整 LP depth/退出雷达 |
| Contract Permission | missing | 无 owner/proxy/admin 权限变更生产雷达 |
| Project Relationship | partial | 领域模型/Repository 边界存在，权威关系采集尚未实现 |
| Wallet Profile | partial | 钱包群组与本地行为候选可用，不等于跨 Token 身份画像 |
| Cross-token | missing | 无跨 Token 钱包画像/关联基线 |
| AI | production_ready | Provider、Schema、缓存、降级已生产验收；默认关闭 |
| Telegram | production_ready | Observe/Dry-run/Real、双门禁、路由、卡片生命周期、查询 Worker |
| Market Convergence | production_ready | 只读现有市场事实，不重复采集 |
| Historical Baseline | production_ready | median/MAD、连续覆盖、rolling facts，完整扫描才写入 |
