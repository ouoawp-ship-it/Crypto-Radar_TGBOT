# OAR-P2：Token 行为与钱包关联分析

OAR-P2 在 OAR-P1 的 Base ERC-20 `token-activity` 查询结果上执行一次性、
内存内的规则分析。它不新增 RPC 请求，不写数据库，不创建 Telegram
客户端，也不把钱包永久合并成实体。

## 命令

```bash
python onchain_main.py token-analysis \
  --chain base \
  --contract 0x... \
  --window 24h \
  --allow-network \
  --pretty
```

`token-analysis` 与 `token-activity` 共享以下参数和安全边界：

- `--chain`：第一版只允许 `base`。
- `--contract`：严格校验的 20-byte EVM 合约地址。
- `--window`：`15m`、`1h`、`4h` 或 `24h`。
- `--allow-network`：缺失时不会创建 RPC 客户端。
- `--max-events`、`--max-rpc-requests`、`--top`：继承 OAR-P1 的有界预算。
- `--with-price`、`--min-usd`：价格默认关闭；价格只用于当前估值展示。
- `--pretty`：格式化 JSON。
- `--output-file`：复用 OAR-P1 的非覆盖安全写入；仓库内只允许
  `reports/onchain/`。

命令不接受 `--send` 或 `--confirm-real-send`，永远不会发送 Telegram。

## 输入门禁与嵌套窗口

分析只读取 OAR-P1 已返回的 Transfer 事实。窗口终点始终使用链上查询结果
中的 `query.to_time`，不使用本机时间：

| 查询窗口 | 输出的嵌套窗口 |
|---|---|
| 15m | 15m |
| 1h | 15m、1h |
| 4h | 15m、1h、4h |
| 24h | 15m、1h、4h、24h |

零金额 Transfer 从分析中排除。mint/burn 保留统计，但不作为 CEX 方向或
钱包关联证据。所有 Token 数量、占比和可选美元金额均使用 `Decimal`；
JSON 中的 Decimal 使用字符串。

只有 OAR-P1 `complete=true` 时，才会输出正式行为候选和中高等级钱包
关联。如果输入为 partial：

- 顶层事实仍保留；
- `analysis.status=partial_input`；
- `analysis.complete=false`；
- `primary_behavior.type=insufficient_data`；
- 不输出正式吸筹或派发候选；
- 钱包关联分数上限为 39；
- `limitations` 包含 `query_incomplete`。

## 行为类型

### 未发现近期活动

窗口内没有正数金额的 Transfer 时输出 `no_activity`。

### 偶发行为

`isolated` 只用于输入完整、最大窗口内仅有 1～2 笔相关 Transfer、且活动
只落在一个 15m 桶内的低频事件。它还要求没有正式行为候选、重复 CEX
方向、多对一或一对多图形。

### 证据不足的活动

存在活动但不符合上述严格偶发边界，也没有正式候选时输出
`inconclusive_activity`，并设置
`analysis.status=insufficient_evidence`。例如三笔以上的零散活动、跨多个
15m 桶的弱活动、内部调拨或大量未分类活动，都不会再被写成“偶发行为”。

### 持续吸筹候选

`accumulation_candidate` 仅将高置信度 CEX `outflow` 作为正向证据：

- 查询至少为 1h 且输入完整；
- 标签覆盖状态为 `ok`；
- outflow 至少 3 笔，方向占比至少 0.67；
- 至少两个外部接收钱包，或同一接收钱包至少重复 3 笔；
- 1h 至少覆盖两个 15m 桶，4h/24h 至少覆盖三个；
- 分数至少 55。

反向 inflow 占比过高，以及 internal、cross-CEX、CEX consolidation
占比过高，都会成为反证并扣分。

“从交易所提出”不等于已经买入，也不证明一定上涨或长期持有。

### 持续派发候选

`distribution_candidate` 与吸筹规则对称，使用高置信度 CEX `inflow`。
“流入交易所”只表示潜在可售供应增加，不等于已经卖出，也不证明必然下跌。

### 多钱包归集候选

`wallet_consolidation_candidate` 表示至少三个不同钱包向同一非零、
非方向分类 CEX 地址转账，至少 3 笔，且组内金额达到窗口非 mint/burn
Token 数量的 10%。未知目标会标记 `target_role_unknown`。

一般归集只使用 `non_cex` 或 `unclassified` Transfer，并要求两端都不是
方向分类 CEX。它与现有 Deposit → Hot/Collector 的 CEX consolidation
完全分开。

### 批量分发候选

`fanout_candidate` 表示同一非零、非方向分类 CEX 地址向至少三个不同地址
转账，至少 3 笔，且达到相同金额占比门槛。未知发送方会标记
`sender_role_unknown`，结果不得解释成发送方控制所有接收钱包。

一般分发同样只使用 `non_cex` 或 `unclassified` Transfer。CEX inflow、
outflow、internal、consolidation 和 cross-CEX 不进入一般归集/分发候选。

一般归集或分发的成员超过 20 时，会增加 `batch_or_airdrop_possible`，
行为分数封顶 69 且置信度固定为 `low`；候选事实仍会保留。

候选可以并存。`primary_behavior` 按分数、支持证据数量和固定类型顺序选择：

1. distribution_candidate
2. accumulation_candidate
3. wallet_consolidation_candidate
4. fanout_candidate
5. isolated / inconclusive_activity（仅作为无正式候选时的 fallback）

## 钱包候选群组

第一版只生成查询窗口内的候选，不做全局或传递性合并：

- `shared_target`：多个钱包转入同一非 CEX/未知目标。
- `shared_source`：同一非 CEX/未知来源向多个钱包分发。
- `synchronized_cex_inflow`：多个外部钱包在同步窗口内向同一家 CEX 转入。
- `synchronized_cex_outflow`：同一家 CEX 在同步窗口内向多个外部钱包转出。

同一个钱包可以出现在多个群组。A 关联 B、B 关联 C 不会自动推导 A/B/C
属于同一主体。`group_id` 是基于 chain、Token 合约、窗口、群组类型、
排序后钱包、群组锚点/交易所和算法版本生成的稳定 SHA-256。

## 关联评分

评分只用于排序可解释规则证据，`score_semantics` 固定为
`rule_score_not_probability`：

- 共享非 CEX 目标或来源：+30
- 多个嵌套窗口存在独立的较早事件和新增 15m 桶：+20
- 时间在配置的同步窗口内：+15
- 金额差异在配置容差内：+15
- 群组成员间存在直接 Token Transfer：+10
- 同步流入或流出同一家 CEX：+10

等级：

| 分数 | 等级 |
|---:|---|
| 0–19 | 证据不足 |
| 20–39 | 弱关联 |
| 40–59 | 中等概率关联 |
| 60–79 | 高概率关联 |
| 80–100 | 强关联候选 |

以下限制会封顶：

- 唯一共同点是同一家 CEX：最高 39。
- 支持事件少于 3：最高 19。
- 只有一种证据类型：最高 39。
- 输入 partial：最高 39。
- 群组成员超过 20：最高 39，并提示批量/空投可能。
- 分析预算耗尽：最高 59，`analysis.status=partial_analysis`。

这些等级不是控制权概率，更不能表述为“已确认同一主力”或“钱包已合并”。

`repeated_across_nested_windows` 不按窗口名称计数。同一批事件同时出现在
15m、1h、4h 或 24h 窗口时只算一次；只有较长窗口包含较短窗口之外的
较早 source event、形成新增 15m 桶，并保持同一方向或结构签名时，才会
增加跨窗口持续证据。钱包群组使用相同的独立事件判定。

## 输出

OAR-P1 的全部事实字段保持不变，`token-analysis` 只新增 `analysis`：

```json
{
  "analysis": {
    "schema_version": 1,
    "algorithm_version": "oar-behavior-v1",
    "wallet_group_algorithm_version": "oar-wallet-group-v1",
    "status": "ok",
    "complete": true,
    "input_complete": true,
    "score_semantics": "rule_score_not_probability",
    "valuation_basis": "token_amount",
    "windows": {},
    "primary_behavior": {},
    "behavior_candidates": [],
    "coexisting_behavior_types": [],
    "observed_patterns": [],
    "wallet_groups": [],
    "limitations": []
  }
}
```

没有价格时继续按 Token 数量分析。显式启用价格后，
`valuation_basis=current_usd_estimate`，美元值仅是查询时可用价格的估算，
不会改变行为方向，也不是历史成交价格。

## 配置与预算

安全默认值记录在 `.env.onchain.example`。配置校验包含硬上限：

- 分析钱包最多 200；
- 输出钱包群组最多 50；
- 单项来源事件 ID 最多 200；
- 同步窗口最多 1800 秒；
- 金额相似容差最多 0.50；
- 方向占比范围为 0.5–1；
- 金额份额范围为 0–1。

分析先按地址、CEX、flow type 和 15m 桶分组，再在有限候选组内检查证据，
不会对全部钱包执行无界两两比较。

## 零副作用边界

`token-analysis` 不执行 Migration，不推进 chain cursor，不写
`processed_blocks`、Transfer、flow、snapshot、alert、delivery、
`signals.db`、Telegram history 或 outbox。它不创建 `TelegramGateway`、
`OnchainNotifier` 或 AI 客户端。

## 明确限制与 OAR-P3 边界

第一版仅支持 Base ERC-20 Transfer，不包含原生币/Trace、Gas 资金来源、
多签 signer、Owner/Admin、部署者、完整 DEX 路径或跨链桥路径。标签覆盖
有限；高密度 Token 的 OAR-P1 输入可能为 partial，此时分析严格降级。

OAR-P3 才会讨论 AI 解释和独立 Telegram 话题。本阶段没有实现或启用这些
能力。
