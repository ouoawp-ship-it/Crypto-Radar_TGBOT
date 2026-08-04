# OAR 目标领域架构

目标是把现有生产能力收敛成单向依赖的六层领域架构，而不是复制一套“P7 OAR”。旧入口在
迁移期通过 adapter/compatibility wrapper 复用同一实现。

## 依赖方向

```text
CLI / systemd / ConfigManager
            |
            v
Application orchestration (query, watch, report, review)
            |
            v
Domain layers 1 -> 2 -> 3 -> 4 -> 5 -> 6
            ^                         |
            |                         v
Injected RPC / repositories / market reader / notification gateway
```

Domain 不读取环境变量、不创建 HTTP client、不打开主 BOT 数据库、不依赖 CLI/Telegram。
时钟、事实 Provider、Repository 和 Gateway 均由 application 层注入。

## Layer 1 — Chain Facts

事实包括 Block、Transaction、Receipt、Transfer、Balance、Token Metadata、totalSupply、
mint/burn、Pool/Contract Event、completeness 和 reorg/finality。该层只描述“发生了什么”，
不得输出吸筹、派发、项目方、做市商或交易建议。

现有落点：`NormalizedTransfer`、`JsonRpcClient`、Adaptive Range、
`TokenActivityQueryService`、`TokenMetadataResolver`。P7 新增
`EvmTokenSnapshotProvider`，在 Transfer 前一块和当前块读取 sender balance，并在同一
评估 block 读取 totalSupply；调用预算和 TTL 缓存独立可审计。

## Layer 2 — Identity and Relationships

身份分为：

- `verified_identity`：链上可确定 metadata/contract identity；
- `reviewed_label`：人工批准且未过期的地址标签；
- `deterministic_relationship`：可由 deploy/owner/admin/vesting/initial LP 事实证明；
- `behavior_candidate`：只作为审核辅助；
- `unclassified`：证据不足。

`AddressLabelRepository` 只暴露 reviewed 标签；`ProjectRelationshipRepository` 是 P7 的
稳定边界，第一轮不伪造项目关系采集器。行为候选永远不能自动变为 CEX 身份。

## Layer 3 — Derived Metrics

从完整事实和可用快照确定性计算：gross inflow/outflow、net flow、sender exit/remaining
ratio、total/circulating supply share、CEX balance、holder concentration、LP depth、钱包
同步及 historical deviation。缺失值保留为 unavailable，不按 0 参与评分。

P7 首轮实现 sender exit/remaining、total supply share 和可注入的 circulating share；
holder、LP 和 circulating supply 权威来源留在后续阶段。

## Layer 4 — Deterministic Signals

持续行为仍由 P2 `BehaviorAnalyzer` 独立评分。P7 新增 `SingleTransferRiskEngine`，第一版
输出：`large_cex_inflow`、`large_cex_outflow`、`near_full_exit_to_cex`、
`full_exit_to_cex`、`project_related_cex_inflow`、`unlock_related_cex_inflow`。

正式门禁为：

```text
actionable = sustained_behavior_gate OR single_transfer_risk_gate
```

两个引擎分别保存规则分、证据、反证、完整性和限制，不合成黑盒分数。统一信号字段为：

- `rule_score` 和 `score_semantics=rule_score_not_probability`；
- `level`: info/watch/important/high_risk/critical；
- `evidence_strength`、`historical_anomaly`、`data_completeness`；
- `identity_coverage`、`counter_evidence`、`limitations`。

只要 query partial、非 finalized/reorg 风险、快照预算失败或关键事实不一致，就不形成正式
actionable 信号。

## Layer 5 — Convergence

通过 `MarketContextReader` 只读已有 Price、Spot/Futures Volume、OI、Funding、Basis、
Active Buy/Sell、Order Book、Liquidation 和 Ranking。禁止在 OAR 内复制主项目市场采集器。
现有 `market_convergence.py` 和 `signal_bridge.MainSignalReader` 继续作为 adapter。

## Layer 6 — Interpretation and Delivery

规则报告始终可用；AI 是可选解释层，默认关闭、失败可降级。Telegram 继续使用专用 template、
topic、history、outbox、hourly quota 和 card lifecycle；真实发送必须双门禁。P7 新模板只有
formatter/fixture，不连接 Gateway：单笔入所/出所、近乎清仓、项目关联入所、持续 CEX
净流、多钱包同步入所、综合共振、数据/标签不足降级。

## 稳定接口与实际理由

`domain.py` 定义：`ChainFactProvider`、`TokenSnapshotProvider`、
`AddressLabelRepository`、`ProjectRelationshipRepository`、`TransferClassifier`、
`SingleTransferRiskEnginePort`、`BehaviorAnalysisEngine`、`RollingMetricRepository`、
`SignalPolicy`、`MarketContextReader`、`ReportFormatter`、`NotificationGateway`。

这些接口只出现在已经需要 Fake 替换、存在旧/新 adapter，或需要切断 CLI/HTTP/文件依赖的
边界。没有为每个函数创建抽象；纯领域函数保持普通函数。

## EVM 共享接入与非 EVM 边界

Base 保持现有 production-ready 路径。BSC、Ethereum、Arbitrum、Optimism、Polygon 和
Avalanche C-Chain 使用同一个 `EvmChainSpec`、RPC、metadata、Registry、Token
Activity/Analysis、classifier、single-risk 和 formatter，不按链复制 Watch、DB、Telegram
或行为引擎。`ChainRef` 使用 `eip155:<chain_id>` 作为跨适配器稳定链身份；每条新增链均有
独立 enable、RPC 和 Explorer 配置，默认关闭，未做真实 RPC 灰度。

BSC 初始配置：chain ID 56、独立 enable/RPC/confirmation/reorg/budget、BscScan explorer、
WBNB、稳定币和 PancakeSwap V2/V3 factory/router。BSC 默认关闭，未配置 RPC 时 readiness
只报脱敏 blocked/disabled；第一轮不做真实 RPC 灰度。静态地址来自 BNB Chain 官方网络
说明及 PancakeSwap 官方部署/默认 token list，任何生产启用仍需 P7B 复核。

Solana、Tron、Bitcoin、Sui、TON 与 EVM 的地址、资产、事件和 finality 模型不同。本轮只把
链身份从裸整数提升为 namespace-aware `ChainRef`，不把它们伪装成“已支持”。后续适配器必须
先落地各自的 Address Codec、Asset Identity、Event Locator 和 Fact Provider；Bitcoin 还必须
使用独立 UTXO 事实模型。它们可以复用 Watch 调度、Lease、Completeness、Signal Policy、
Report 和 Card Lifecycle，但不能复用 ERC-20 `eth_getLogs` 解析器或 20-byte 地址校验。

审计参考：

- [BNB Chain 官方 BSC 网络配置](https://docs.bnbchain.org/bnb-smart-chain/developers/wallet-configuration/)
- [BNB Chain 官方 JSON-RPC 说明](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/)
- [PancakeSwap V2 官方部署地址](https://developer.pancakeswap.finance/contracts/v2/addresses)
- [PancakeSwap V3 官方部署地址](https://developer.pancakeswap.finance/contracts/v3/addresses)
- [PancakeSwap 官方默认 Token List](https://raw.githubusercontent.com/pancakeswap/token-list/main/lists/pancakeswap-default.json)

## 存储目标与迁移策略

- `onchain_facts.db`：未来仅由 facts repository 写 Transfer、Balance/Supply/Holder/Pool/
  Contract Event/Project Relationship；
- `oar_automation.db`：继续保存 Registry/Watch/Lease/Audit/Baseline/Rolling/queue metadata；
- `onchain_signals.db`：继续保存 deterministic signal、AI 摘要、delivery/card lifecycle；
- 地址情报：继续独立候选、证据和人工审核边界；
- 主 `signals.db`：始终只读。

本轮不创建空的 `onchain_facts.db`，不双写，也不迁移 `onchain_flow.db` 历史。先通过
repository 接口停止新领域逻辑直接依赖具体文件；P7B/P7C 设计带版本、校验、备份和回滚的
单向迁移。旧库继续可读，历史审计不受影响。

## 配置治理

Single Transfer Risk 和所有新增链均默认关闭。可运维调整的只有 enable、明确分数/ratio/share
门槛、快照预算/TTL，以及链级 RPC/confirmation/reorg。ConfigManager 对布尔、整数、
Decimal 和 HTTPS credential-free RPC 进行范围与组合校验，锁定、备份、原子写入、写后
校验和失败回滚保持不变。领域代码只接收已验证 settings/value objects。

## 分阶段落地

1. P7A（本轮）：文档审计、领域边界、Base snapshot、单笔风险、七条 EVM 链离线接入、formatter；非 EVM 只完成真实边界说明。
2. P7B：按 BSC、Ethereum、Arbitrum、Optimism、Polygon、Avalanche 的顺序逐链做真实 Observe 灰度、链级预算/重组/Registry 生产证据。
3. P7C：Holder 与筹码集中，明确索引成本和数据缺口。
4. P7D：DEX LP，分别处理 V2 reserve 和 V3 concentrated liquidity。
5. P7E：项目关系、owner/proxy/admin/vesting 与权限事件。
6. P7F：跨 Token 钱包画像，不自动合并现实身份。
7. P7G：庄票市场共振，只读市场事实并保留链上反证。
